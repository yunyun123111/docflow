"""归档服务：移动优先 + 防覆盖 + 操作留痕；SHA256 去重；绝不删原件。"""
from __future__ import annotations

import hashlib
import os
import shutil
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import Document, OperationLog, STATUS_ARCHIVED
from backend.utils.fs import fs_path, unique_filename
from backend.utils.logger import get_logger

logger = get_logger("archive")


class FileOpError(Exception):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(fs_path(path), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def move_file(src: Path, dst: Path, allow_overwrite: bool | None = None) -> Path:
    """移动（同卷原子 rename，跨卷 copy+delete 带回滚），默认拒绝覆盖。"""
    if not src.is_file():
        raise FileOpError(f"源文件不存在: {src}")
    if src.resolve() == dst.resolve():
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    allow = settings.ALLOW_OVERWRITE if allow_overwrite is None else allow_overwrite
    backup: Path | None = None
    if dst.exists():
        if not allow:
            raise FileOpError(f"目标已存在，拒绝覆盖: {dst}")
        backup = dst.with_name(f"{dst.name}.{int(datetime.now().timestamp() * 1000)}.replacing")
        os_replace(dst, backup)
    try:
        try:
            shutil.move(fs_path(src), fs_path(dst))
        except shutil.Error:
            shutil.copy2(fs_path(src), fs_path(dst))
            os_remove(src)
    except OSError as e:
        if dst.exists() and not src.exists() and backup is None:
            try:
                os_remove(dst)
            except OSError:
                pass
        elif backup is not None and not dst.exists():
            try:
                os_replace(backup, dst)
            except OSError:
                logger.critical("覆盖失败且备份滞留 %s（文件未丢失）", backup)
        raise FileOpError(f"移动失败: {e}") from e
    if backup is not None:
        try:
            os_remove(backup)
        except OSError:
            logger.warning("覆盖备份清理失败: %s", backup)
    return dst


def os_replace(src: Path, dst: Path) -> None:
    os.replace(fs_path(src), fs_path(dst))


def os_remove(p: Path) -> None:
    os.remove(fs_path(p))



def log_operation(db: Session, op: str, *, document_id: int | None = None,
                  old_path: str = "", new_path: str = "", detail: str = "",
                  result: str = "ok", error: str = "") -> OperationLog:
    row = OperationLog(op=op, document_id=document_id, old_path=old_path,
                       new_path=new_path, detail=detail, result=result, error=error)
    db.add(row)
    return row


def archive_document(db: Session, doc: Document, segments: list[str], new_name: str) -> Path:
    """归档到 DOCUMENT_ROOT/segments/new_name。返回最终路径。"""
    target_dir = Path(settings.document_root, *segments)
    target = unique_filename(target_dir, new_name)
    src = Path(doc.current_path or doc.original_path)
    old = str(src)
    move_file(src, target)
    doc.current_path = str(target)
    doc.current_filename = target.name
    doc.status = STATUS_ARCHIVED
    doc.archived_at = datetime.now()
    log_operation(db, "archive", document_id=doc.id, old_path=old, new_path=str(target),
                  detail="/".join(segments))
    logger.info("归档: %s -> %s", old, target)
    return target