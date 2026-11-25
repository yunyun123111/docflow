"""安全回归：目录穿越 / zip-slip / 令牌与会话 / 文件名守卫。"""
from __future__ import annotations

import io
import zipfile

from backend.config import BASE_DIR, settings
from backend.utils.fs import safe_join


def test_safe_join_blocks_traversal():
    base = settings.temp_dir
    ok = safe_join(base, "a", "b.txt")
    assert str(ok).startswith(str(base))
    for bad in ("../../evil.txt", "..\\..\\evil.txt", "a/../../../windows/win.ini"):
        try:
            safe_join(base, bad)
            raise AssertionError(f"应被拒绝: {bad}")
        except ValueError:
            pass


def test_spa_route_no_traversal(client):
    # 旧版可经 %2F 编码穿越读取 dist 外文件；新版守卫必须拦住任何泄漏
    markers = ("DATABASE_URL", "AI_API_KEY", "sqlite:///", "WATCH_DIRS")
    for path in ("/..%2F..%2F.env", "/%2e%2e/%2e%2e/.env", "/../../.env",
                 "/assets/..%2F..%2Fbackend%2Fconfig.py"):
        r = client.get(path)
        leaked = [m for m in markers if m in r.text]
        assert not leaked, (path, r.status_code, leaked)


def test_unknown_api_returns_json_404(client):
    r = client.get("/api/definitely-not-here")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


def _malicious_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # 非法恢复包：缺 app.db 也应拒绝；成员含穿越路径
        zf.writestr("documents/../../../../evil_escape.txt", "bad")
        zf.writestr("database/app.db", "not-a-sqlite-file-but-placeholder")
    return buf.getvalue()


def test_restore_rejects_zip_slip(client):
    import sqlite3

    good = io.BytesIO()
    with zipfile.ZipFile(good, "w") as zf:
        db = sqlite3.connect(":memory:")
        p = settings.temp_dir / "_mk.db"
        con = sqlite3.connect(str(p))
        con.execute("CREATE TABLE t(x)")
        con.commit()
        con.close()
        zf.write(p, "database/app.db")
        p.unlink()
        zf.writestr("meta.json", "{}")
        zf.writestr("documents/../../../../evil_escape.txt", "bad")
    r = client.post("/api/backup/restore", files={"file": ("backup_evil.zip", good.getvalue(), "application/zip")})
    assert r.status_code == 400, r.text
    assert "非法成员" in r.json()["detail"] or "越" in r.json()["detail"]
    escaped = BASE_DIR.parent.parent.parent / "evil_escape.txt"
    assert not escaped.exists()


def test_backup_name_guard(client):
    r = client.delete("/api/backup/..%5C..%5Crun.py")
    assert r.status_code in (400, 404)
    r = client.get("/api/backup/download/backup_20990101_000000.zip")
    assert r.status_code == 400


def test_upload_rejects_unsupported_and_cleans(client, tmp_base):
    r = client.post("/api/documents/upload",
                    files=[("files", ("evil.exe", io.BytesIO(b"MZ123"), "application/octet-stream"))])
    assert r.status_code == 200
    assert r.json()["rejected"]
    import glob
    import os

    leftovers = [f for f in os.listdir(settings.inbox_root) if "evil" in f]
    assert not leftovers


def test_session_token_flow(client):
    from backend.security import check_session, issue_token, revoke_token

    tk = issue_token()
    assert check_session(tk)
    revoke_token(tk)
    assert not check_session(tk)
    assert not check_session("static-derived-token")  # 猜测令牌无效


def test_password_login_rate_limit(client):
    from backend import security

    settings.ACCESS_PASSWORD = "unit-test-pw"
    try:
        results = [client.post("/api/auth/login", json={"password": "wrong"}).status_code
                   for _ in range(security.MAX_ATTEMPTS_PER_MINUTE + 2)]
        assert 429 in results
        security._login_attempts.clear()
        ok = client.post("/api/auth/login", json={"password": "unit-test-pw"})
        assert ok.status_code == 200
        if ok.status_code == 200:
            token = ok.json()["token"]
            assert token
            r = client.get("/api/documents", headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 200
            r = client.get("/api/documents")
            assert r.status_code == 401
    finally:
        settings.ACCESS_PASSWORD = ""