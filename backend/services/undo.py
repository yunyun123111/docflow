"""撤销：按操作日志逆向恢复文件位置与状态。

撤销语义：对某文档从最近一条未撤销的 archive/rename/move 日志开始逆向回退，
把文件从 new_path 移回 old_path（防重名，不覆盖），状态回到 need_review/pending。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from backend.models import Document, OperationLog, STATUS_NEED_REVIEW
from backend.services.archive import move_file, log_operation
from backend.utils.fs import unique_filename
from backend.utils.logger import get_logger

logger = get_logger("undo")
REVERSIBLE_OPS = ("archive", "rename", "move")


class UndoError(Exception):
    pass


def undo_document(db: Session, doc: Document) -> int:
    """撤销该文档的全部可逆操作（由新到旧）。返回撤销条数。"""
    logs = (db.query(OperationLog)
            .filter(OperationLog.document_id == doc.id,
                    OperationLog.op.in_(REVERSIBLE_OPS),
                    OperationLog.result == "ok",
                    OperationLog.undone_at.is_(None))
            .order_by(OperationLog.id.desc()).all())
    if not logs:
        raise UndoError("没有可撤销的操作")
    count = 0
    for row in logs:
        src, dst = Path(row.new_path), Path(row.old_path)
        if not src.is_file():
            raise UndoError(f"当前文件不存在，无法撤销: {src}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        target = unique_filename(dst.parent, dst.name) if dst.exists() and target_needs_new_name(src, dst) else dst
        move_file(src, target)
        row.undone_at = datetime.now()
        doc.current_path = str(target)
        doc.current_filename = target.name
        count += 1
    doc.status = STATUS_NEED_REVIEW
    doc.archived_at = None
    log_operation(db, "undo", document_id=doc.id, detail=f"逆向 {count} 条操作")
    db.commit()
    logger.info("撤销 doc#%s，共 %s 步", doc.id, count)
    return count


def target_needs_new_name(src: Path, dst: Path) -> bool:
    return src.resolve() != dst.resolve()