"""备份：zip 快照（DB 一致性拷贝 + 可选文档）；恢复带成员路径守卫；自动轮转。"""
from __future__ import annotations

import json
import shutil
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

from backend.config import BASE_DIR, settings
from backend.utils.fs import safe_join
from backend.utils.logger import get_logger

logger = get_logger("backup")

MEM_DB = "database/app.db"
MEM_ENV = "env/.env"
MEM_DOC = "documents"
MEM_LEDGER = "ledgers"
MEM_SAMPLE = "samples"
MEM_META = "meta.json"


class BackupError(Exception):
    pass


def _backup_dir() -> Path:
    settings.backup_root.mkdir(parents=True, exist_ok=True)
    return settings.backup_root


def _db_file() -> Path:
    return Path(settings.DATABASE_URL.replace("sqlite:///", ""))


def create_backup(include_documents: bool = False) -> dict:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"backup_{stamp}.zip"
    out = _backup_dir() / name
    tmp_db = settings.temp_dir / f"_bk_{stamp}.db"
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(_db_file()))
    dst = sqlite3.connect(str(tmp_db))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(tmp_db, MEM_DB)
        env = BASE_DIR / ".env"
        if env.exists():
            zf.write(env, MEM_ENV)
        zf.writestr(MEM_META, json.dumps({
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "include_documents": include_documents,
            "version": 2,
        }, ensure_ascii=False))
        counts: dict[str, int] = {"documents": 0, "ledgers": 0, "samples": 0}
        if include_documents:
            for sub, mem in ((settings.document_root, MEM_DOC), (settings.ledger_root, MEM_LEDGER),
                             (settings.sample_root, MEM_SAMPLE)):
                if sub.is_dir():
                    for p in sub.rglob("*"):
                        if p.is_file():
                            zf.write(p, f"{mem}/{p.relative_to(sub)}")
                            counts[mem.rstrip("s")] = counts.get(mem.rstrip("s"), 0) + 1
    tmp_db.unlink(missing_ok=True)
    prune()
    logger.info("备份完成: %s (%s)", name, counts)
    return {"filename": name, "size": out.stat().st_size, **counts}


def prune() -> int:
    files = sorted(_backup_dir().glob("backup_*.zip"), key=lambda p: p.name)
    removed = 0
    while len(files) > max(1, settings.AUTO_BACKUP_KEEP):
        files.pop(0).unlink()
        removed += 1
    return removed


def list_backups() -> list[dict]:
    out = []
    for p in sorted(_backup_dir().glob("backup_*.zip"), reverse=True):
        meta = {}
        try:
            with zipfile.ZipFile(p) as zf:
                meta = json.loads(zf.read(MEM_META)) if MEM_META in zf.namelist() else {}
        except zipfile.BadZipFile:
            meta = {"error": "损坏"}
        out.append({"filename": p.name, "size": p.stat().st_size, **meta})
    return out


def get_backup_path(filename: str) -> Path:
    try:
        target = safe_join(_backup_dir(), filename)
    except ValueError as e:
        raise BackupError(str(e)) from e
    if target.name.startswith("backup_") and target.suffix == ".zip" and target.is_file():
        return target
    raise BackupError(f"备份不存在: {filename}")


def delete_backup(filename: str) -> None:
    get_backup_path(filename).unlink()
    logger.info("删除备份: %s", filename)


def _safe_member(name: str) -> bool:
    parts = Path(name).parts
    return not (Path(name).is_absolute() or ".." in parts or not parts)


def restore_backup(zip_path: Path, include_documents: bool = True) -> dict:
    if not zip_path.is_file():
        raise BackupError("备份文件不存在")
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        if MEM_DB not in names:
            raise BackupError("备份包缺少数据库")
        bad = [n for n in names if not _safe_member(n)]
        if bad:
            raise BackupError(f"备份包含非法成员路径: {bad[:3]}")
        tmp = settings.temp_dir / "_restore.db"
        settings.temp_dir.mkdir(parents=True, exist_ok=True)
        with zf.open(MEM_DB) as s, open(tmp, "wb") as d:
            shutil.copyfileobj(s, d)
        src = sqlite3.connect(str(tmp))
        dst = sqlite3.connect(str(_db_file()))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        tmp.unlink(missing_ok=True)

        restored = 0
        if include_documents:
            for n in names:
                if n.startswith(f"{MEM_DOC}/") and not n.endswith("/"):
                    rel = n[len(MEM_DOC) + 1:]
                    dest = safe_join(settings.document_root, rel)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(n) as s, open(dest, "wb") as d:
                        shutil.copyfileobj(s, d)
                    restored += 1
                elif n.startswith(f"{MEM_LEDGER}/") and not n.endswith("/"):
                    rel = n[len(MEM_LEDGER) + 1:]
                    dest = safe_join(settings.ledger_root, rel)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(n) as s, open(dest, "wb") as d:
                        shutil.copyfileobj(s, d)
        env_restored = False
        env = BASE_DIR / ".env"
        if MEM_ENV in names:
            with zf.open(MEM_ENV) as s, open(env, "wb") as d:
                shutil.copyfileobj(s, d)
            env_restored = True
    from backend.database import engine as _engine

    _engine.dispose()
    from backend.database import init_db

    init_db()
    logger.info("恢复完成: %s（文档 %s 份）", zip_path.name, restored)
    return {"ok": True, "restored_documents": restored, "env_restored": env_restored,
            "note": "配置已恢复，建议重启服务使配置完全生效"}


def auto_backup_tick() -> bool:
    """由调度线程调用：跨天才备份。"""
    if not settings.AUTO_BACKUP_ENABLED:
        return False
    latest = next(iter(list_backups()), None)
    today = datetime.now().strftime("%Y%m%d")
    if latest and latest["filename"].startswith(f"backup_{today}"):
        return False
    create_backup(include_documents=False)
    return True