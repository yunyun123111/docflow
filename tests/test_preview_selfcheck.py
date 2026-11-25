"""预览（文档库/人工审核）+ 配置自检 + 桌面版启动器辅助函数。"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


@pytest.fixture()
def pdf_doc(client, db):
    """在归档目录里造一个两页 PDF 并入库，返回 (doc_id, path)。"""
    from backend.config import settings
    from backend.models import Category, Document, DocumentText

    settings.ensure_dirs()
    path = settings.document_root / "预览样例.pdf"
    import fitz

    doc = fitz.open()
    for n in (1, 2):
        page = doc.new_page()
        page.insert_text((72, 90), f"DocFlow preview sample page {n} HT-2026-0099", fontsize=13)
    doc.save(str(path), garbage=3)
    doc.close()

    cat = db.query(Category).first() or Category(name="合同", path="合同")
    if cat.id is None:
        db.add(cat)
        db.flush()
    d = Document(original_filename=path.name, current_filename=path.name,
                 original_path=str(path), current_path=str(path), file_type="pdf",
                 file_size=path.stat().st_size, document_type="合同", title="预览样例",
                 status="archived", confidence=0.9, suggested_category_id=cat.id)
    db.add(d)
    db.flush()
    db.add(DocumentText(document_id=d.id, text="合同编号 HT-2026-0099 金额 10000 元",
                        text_spaced="合同编号 HT-2026-0099 金额 10000 元"))
    db.commit()
    return d.id, path


def test_pdf_preview_meta_and_pages(client, pdf_doc):
    doc_id, _ = pdf_doc
    meta = client.get(f"/api/preview/{doc_id}").json()
    assert meta["kind"] == "pdf" and meta["exists"] and meta["pages"] == 2
    assert meta["mime"] == "application/pdf"
    assert meta["file_url"].endswith("/file")

    thumb = client.get(f"/api/preview/{doc_id}/thumbnail", params={"size": 200})
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/png"
    assert thumb.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert thumb.headers.get("etag")

    # 第二次命中磁盘缓存，内容一致
    assert client.get(f"/api/preview/{doc_id}/thumbnail", params={"size": 200}).content == thumb.content

    page2 = client.get(f"/api/preview/{doc_id}/page/1", params={"width": 700})
    assert page2.status_code == 200 and len(page2.content) > 500
    assert client.get(f"/api/preview/{doc_id}/page/99").status_code == 200  # 越界页码夹到末页

    body = client.get(f"/api/preview/{doc_id}/text", params={"start": 0, "length": 200}).json()
    assert body["total_length"] == len("合同编号 HT-2026-0099 金额 10000 元")
    assert body["body"].startswith("合同编号") and body["has_more"] is False
    assert client.get(f"/api/preview/{doc_id}/text", params={"start": 0, "length": 5}).status_code == 422


def test_preview_file_inline_and_range(client, pdf_doc):
    doc_id, _ = pdf_doc
    r = client.get(f"/api/preview/{doc_id}/file")
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("inline")
    assert r.headers.get("accept-ranges") == "bytes"
    ranged = client.get(f"/api/preview/{doc_id}/file", headers={"Range": "bytes=0-63"})
    assert ranged.status_code == 206 and len(ranged.content) == 64


def test_table_and_text_preview(client, db):
    from backend.config import settings
    from backend.models import Document

    csv_path = settings.document_root / "台账片段.csv"
    csv_path.write_text("合同编号,金额\nHT-1,100\nHT-2,200\n", encoding="utf-8")
    d = Document(original_filename=csv_path.name, current_filename=csv_path.name,
                 original_path=str(csv_path), current_path=str(csv_path), file_type="csv",
                 file_size=csv_path.stat().st_size, status="archived")
    db.add(d)
    db.commit()
    meta = client.get(f"/api/preview/{d.id}").json()
    assert meta["kind"] == "table"
    assert meta["table"]["columns"] == ["合同编号", "金额"]
    assert meta["table"]["rows"][0] == ["HT-1", "100"]

    xlsx_path = settings.document_root / "结算表.xlsx"
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "结算"
    ws.append(["合同号", "结算金额"])
    ws.append(["HT-9", 5000])
    wb.save(str(xlsx_path))
    x = Document(original_filename=xlsx_path.name, current_filename=xlsx_path.name,
                 original_path=str(xlsx_path), current_path=str(xlsx_path), file_type="xlsx",
                 file_size=xlsx_path.stat().st_size, status="archived")
    db.add(x)
    db.commit()
    meta = client.get(f"/api/preview/{x.id}").json()
    assert meta["table"]["sheets"] == ["结算"]
    assert meta["table"]["columns"] == ["合同号", "结算金额"]


def test_preview_rejects_paths_outside_managed_roots(client, db, tmp_base):
    """预览只能读受管目录内的文件（防止记录被篡改后任意读取磁盘）。"""
    from backend.models import Document

    evil = Path(tmp_base) / "outside.pdf"
    evil.write_bytes(b"%PDF-1.4 fake")
    d = Document(original_filename="outside.pdf", current_filename="outside.pdf",
                 original_path=str(evil), current_path=str(evil), file_type="pdf",
                 file_size=evil.stat().st_size, status="archived")
    db.add(d)
    db.commit()
    meta = client.get(f"/api/preview/{d.id}").json()
    assert meta["exists"] is False and "受管目录" in meta["reason"]
    assert client.get(f"/api/preview/{d.id}/thumbnail").status_code == 404
    assert client.get(f"/api/preview/{d.id}/page/0").status_code == 400


def _make_pdf(path: Path, text: str) -> Path:
    import fitz

    doc = fitz.open()
    doc.new_page().insert_text((72, 90), text, fontsize=13)
    doc.save(str(path), garbage=3)
    doc.close()
    return path


def _register(db, path: Path, status: str = "need_review"):
    from backend.models import Document

    d = Document(original_filename=path.name, current_filename=path.name,
                 original_path=str(path), current_path=str(path), file_type="pdf",
                 file_size=path.stat().st_size, status=status, confidence=0.4)
    db.add(d)
    db.commit()
    return d.id


def test_preview_allows_in_place_organize_source_dir(client, db, tmp_base):
    """「按目录整理」是原地处理：文件留在用户指定的目录里，预览必须能读，
    否则文档库/人工审核里这类文件永远打不开（打包版曾真实复现）。"""
    from backend.models import ProcessingJob

    src = Path(tmp_base) / "待整理-D盘"
    src.mkdir()
    doc_id = _register(db, _make_pdf(src / "原地合同.pdf", "HT-2026-7777"))

    assert client.get(f"/api/preview/{doc_id}").json()["exists"] is False
    db.add(ProcessingJob(source=str(src.resolve()), kind="batch", status="completed", total=1))
    db.commit()

    meta = client.get(f"/api/preview/{doc_id}").json()
    assert meta["exists"] is True and meta["pages"] == 1
    assert client.get(f"/api/preview/{doc_id}/thumbnail").status_code == 200
    # 同级但未被授权过的目录仍然拒绝
    other = Path(tmp_base) / "别处"
    other.mkdir()
    assert client.get(f"/api/preview/{_register(db, _make_pdf(other / 'x.pdf', 'no'))}").json()["exists"] is False


def test_preview_allows_configured_watch_dirs(client, db, tmp_base):
    """无人值守监控目录同样原地处理，配置过就该能预览。"""
    import json

    from backend.config import settings

    watched = Path(tmp_base) / "watched"
    watched.mkdir()
    doc_id = _register(db, _make_pdf(watched / "监控件.pdf", "watched doc"))
    assert client.get(f"/api/preview/{doc_id}").json()["exists"] is False

    original = settings.WATCH_DIRS
    settings.WATCH_DIRS = json.dumps([str(watched)])
    try:
        meta = client.get(f"/api/preview/{doc_id}").json()
        assert meta["exists"] is True and meta["pages"] == 1
    finally:
        settings.WATCH_DIRS = original


def test_selfcheck_reports_and_repairs(client):
    from backend.services import selfcheck

    result = selfcheck.run_checks(quick=True)
    labels = {i["label"] for i in result["items"]}
    for expected in ("运行形态", "配置文件", "界面资源", "数据库", "处理队列"):
        assert expected in labels, expected
    assert result["quick"] is True
    assert set(result["summary"]) == {"ok", "warn", "error", "info"}

    status = client.get("/api/system/status").json()
    assert status["level"] in ("ok", "info", "warn", "error")
    assert isinstance(status["issues"], list)

    for action in ("dirs", "fts", "preview_cache", "requeue", "purge_recycle"):
        r = client.post(f"/api/system/repair/{action}")
        assert r.status_code == 200, (action, r.text)
        assert r.json()["ok"], action
    assert client.post("/api/system/repair/nope").status_code == 400


def test_selfcheck_flags_stuck_processing_documents(client, db, pdf_doc):
    from backend.models import Document
    from backend.services.selfcheck import _check_interrupted

    doc = db.get(Document, pdf_doc[0])
    doc.status = "processing"
    db.commit()
    item = _check_interrupted()
    assert item["level"] == "warn" and item["fix"] == "requeue"
    assert client.post("/api/system/repair/requeue").json()["ok"]
    db.refresh(doc)
    assert doc.status == "pending"


def test_system_info_describes_runtime(client):
    info = client.get("/api/system/info").json()
    assert {"version", "frozen", "mode", "data_dir", "resource_dir", "frontend_bundled"} <= set(info)
    assert info["mode"] == "source"
    assert Path(info["data_dir"]).is_dir()


def test_desktop_launcher_helpers(tmp_path):
    import os

    import desktop_app

    assert desktop_app.pid_alive(os.getpid()) is True
    assert desktop_app.pid_alive(0) is False
    assert desktop_app.pid_alive(-1) is False

    port = desktop_app.free_port()
    assert 1024 < port < 65536
    assert desktop_app.port_busy("127.0.0.1", port) is False

    log = desktop_app.Log(tmp_path / "d.log")
    lock = desktop_app.InstanceLock(tmp_path / ".lock", log)
    assert lock.existing() is None
    lock.acquire(port)
    assert lock.read()["port"] == port
    assert lock.existing()["port"] == port   # 本进程活着 → 视为已有实例
    lock.release()
    assert lock.read() == {}

    # 进程已死的陈旧锁：自动清理并放行
    lock.path.write_text(json.dumps({"pid": 999999, "port": port}), encoding="utf-8")
    assert lock.existing() is None
    assert not lock.path.exists()


def test_webview_flags_keep_window_active(tmp_path, monkeypatch):
    """桌面版必须关掉 WebView2 的遮挡节流，否则窗口挂在后面时界面会停在占位符。"""
    import desktop_app

    monkeypatch.delenv("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", raising=False)
    merged = desktop_app.apply_webview_flags()
    assert "CalculateNativeWinOcclusion" in merged
    assert desktop_app.WEBVIEW_KEEP_ACTIVE in merged
    assert merged == desktop_app.WEBVIEW_KEEP_ACTIVE
    assert "CalculateNativeWinOcclusion" in desktop_app.WEBVIEW_KEEP_ACTIVE

    monkeypatch.setenv("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--remote-debugging-port=9333")
    monkeypatch.delenv("DOCFLOW_WEBVIEW_FLAGS", raising=False)
    merged = desktop_app.apply_webview_flags()
    assert merged.startswith("--remote-debugging-port=9333")   # 用户自己的参数不能被吃掉
    assert desktop_app.WEBVIEW_KEEP_ACTIVE in merged

    # 远程桌面/无显卡环境的逃生口
    monkeypatch.setenv("DOCFLOW_WEBVIEW_FLAGS", "--disable-gpu")
    merged = desktop_app.apply_webview_flags()
    assert "--disable-gpu" in merged and desktop_app.WEBVIEW_KEEP_ACTIVE in merged

    host, got_port, lan, no_window = desktop_app.resolve_startup(["--serve", "--port", "8799"])
    assert (host, got_port, no_window) == ("127.0.0.1", 8799, True)
    assert desktop_app.resolve_startup(["--lan"])[0] == "0.0.0.0"
    assert desktop_app.WINDOW_TITLE.startswith("DocFlow")


def test_desktop_bridge_reveal_validates_document(client, db):
    """桌面桥只接受数据库里的文档 id，路径再经预览守卫。"""
    import desktop_app
    from backend.models import Document

    bridge = desktop_app.Bridge(desktop_app.Log(None))
    assert bridge.reveal_document(999999)["ok"] is False

    doc = db.query(Document).first()
    if doc:
        res = bridge.reveal_document(doc.id)
        assert res["ok"] is True or "找不到" in res["error"] or "受管" in res["error"]
    assert bridge.open_external("javascript:alert(1)")["ok"] is False