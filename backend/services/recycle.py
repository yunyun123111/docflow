"""回收站：软删除（移动到回收站 + 保留期），恢复与彻底删除；支持到期自动清理。

磁盘文件已被外部删除时，"移入回收站" 依然成功：登记一条 recycled_path 为空的记录，
只保留元数据（可追溯、可彻底删除），不再因为 FileNotFound 把用户卡死在库里删不掉。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import (
    Document, RecycleItem, DocumentText, STATUS_ARCHIVED, STATUS_FAILED, STATUS_RECYCLED,
)
from backend.services.archive import FileOpError, log_operation, move_file, os_remove
from backend.utils.fs import unique_filename
from backend.utils.logger import get_logger

logger = get_logger("recycle")

MISSING_NOTE = "（磁盘文件已丢失）"


class RecycleError(Exception):
    pass


def recorded_path(doc: Document) -> Path:
    """数据库里认为文件应在哪里。"""
    return Path(doc.current_path or doc.original_path or "")


def find_file(doc: Document) -> Path | None:
    """真实存在的那个文件：current_path 失效时回看 original_path。"""
    for raw in (doc.current_path, doc.original_path):
        if raw and Path(raw).is_file():
            return Path(raw)
    return None


def doc_file_missing(doc: Document) -> bool:
    return find_file(doc) is None


def item_file_missing(item: RecycleItem) -> bool:
    return not item.recycled_path or not Path(item.recycled_path).is_file()


def to_recycle(db: Session, doc: Document, reason: str = "手动删除") -> RecycleItem:
    """软删除 + 留痕。文件缺失时跳过磁盘移动，仅登记记录。"""
    now = datetime.now()
    src = find_file(doc)
    target: Path | None = None
    if src is not None:
        target_dir = Path(settings.recycle_root, now.strftime("%Y"), now.strftime("%m"))
        target = unique_filename(target_dir, f"{now.strftime('%d_%H%M%S')}_{src.name}")
        try:
            move_file(src, target, allow_overwrite=False)
        except FileOpError as e:
            raise RecycleError(str(e)) from e
    else:
        src = recorded_path(doc)
        logger.warning("文档 %s 的磁盘文件不存在，按“仅清理记录”移入回收站: %s", doc.id, src)
    old = str(src)
    note = "" if target is not None else MISSING_NOTE
    if target is not None:
        # 路径跟着文件走，否则回收站里的预览/缩略图会指向已搬走的旧路径（404）；
        # current_filename 保留原名，回收站内部名（日期前缀）不该露到界面上。
        doc.current_path = str(target)
    retention = settings.RECYCLE_RETENTION_DAYS
    item = RecycleItem(
        document_id=doc.id, name=src.name, original_path=old,
        recycled_path=str(target) if target else "",
        reason=f"{reason}{note}", created_at=now, prev_status=doc.status or "",
        expires_at=now + timedelta(days=retention) if retention > 0 else None,
    )
    db.add(item)
    doc.status = STATUS_RECYCLED
    log_operation(db, "delete", document_id=doc.id, old_path=old,
                  new_path=str(target) if target else "", detail=f"{reason}{note}")
    db.commit()
    return item


def _back_status(db: Session, doc: Document, prev: str) -> str:
    """恢复后该是什么状态：原本已归档就回归档，老记录没留 prev_status 就退回「待整理」。"""
    from backend.models import Category

    if prev != STATUS_ARCHIVED:
        return prev or "pending"
    if doc.archived_category_id and db.get(Category, doc.archived_category_id) is None:
        return "pending"        # 归档分类已经不在了，别挂一个不存在的目录
    return STATUS_ARCHIVED


def restore(db: Session, item: RecycleItem) -> Document | None:
    if not item.recycled_path:
        raise RecycleError("该记录的磁盘文件早已被删除，没有可恢复的文件；可直接彻底删除清理记录")
    src = Path(item.recycled_path)
    if not src.is_file():
        raise RecycleError("回收站文件已丢失（可能被外部清理）；可直接彻底删除清理记录")
    dst = Path(item.original_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    target = unique_filename(dst.parent, dst.name) if dst.exists() else dst
    move_file(src, target)
    doc = db.get(Document, item.document_id) if item.document_id else None
    if doc:
        doc.current_path = str(target)
        doc.current_filename = target.name
        doc.status = _back_status(db, doc, item.prev_status)
        if doc.status == STATUS_ARCHIVED:
            try:
                from backend.services import bundles as bundle_service

                bundle_service.attach_document(db, doc)   # 重新计入业务包与齐备度
            except Exception as exc:  # noqa: BLE001
                logger.warning("恢复后重新入包失败 doc=%s: %s", doc.id, exc)
    log_operation(db, "restore", document_id=doc.id if doc else None,
                  old_path=str(src), new_path=str(target), detail=item.reason)
    db.delete(item)
    db.commit()
    return doc


def purge(db: Session, item: RecycleItem) -> None:
    src = Path(item.recycled_path) if item.recycled_path else None
    if src is not None and src.is_file():
        os_remove(src)
    doc = db.get(Document, item.document_id) if item.document_id else None
    if doc:
        db.query(DocumentText).filter(DocumentText.document_id == doc.id).delete()
        db.delete(doc)
    db.delete(item)
    log_operation(db, "purge", old_path=str(src) if src else item.original_path, detail=item.reason)
    db.commit()


def purge_expired(db: Session) -> int:
    now = datetime.now()
    n = 0
    for item in db.query(RecycleItem).filter(RecycleItem.expires_at.isnot(None),
                                             RecycleItem.expires_at <= now).all():
        try:
            purge(db, item)
            n += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("自动清理失败 %s: %s", item.id, e)
    if n:
        logger.info("自动清理过期回收项 %s 条", n)
    return n