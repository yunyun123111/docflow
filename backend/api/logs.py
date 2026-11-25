"""操作日志 / 错误流水 / 单条撤销。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import Document, OperationLog, STATUS_NEED_REVIEW
from backend.services.undo import undo_document

router = APIRouter(prefix="/api/logs", tags=["logs"])

OP_LABEL = {"import": "导入", "archive": "归档", "rename": "重命名", "move": "移动",
            "delete": "删除(回收站)", "restore": "恢复", "purge": "彻底删除",
            "undo": "撤销", "approve": "审核确认"}


@router.get("")
def listing(op: str | None = None, only_error: bool = False, document_id: int | None = None,
            limit: int = 100, offset: int = 0, db: Session = Depends(get_db)):
    q = db.query(OperationLog)
    if op:
        q = q.filter(OperationLog.op == op)
    if only_error:
        q = q.filter(OperationLog.result == "error")
    if document_id:
        q = q.filter(OperationLog.document_id == document_id)
    total = q.count()
    rows = q.order_by(OperationLog.id.desc()).offset(offset).limit(min(limit, 500)).all()
    return {"total": total, "items": [{
        "id": r.id, "op": r.op, "op_label": OP_LABEL.get(r.op, r.op),
        "document_id": r.document_id, "old_path": r.old_path, "new_path": r.new_path,
        "detail": r.detail, "result": r.result, "error": r.error,
        "undone": bool(r.undone_at),
        "created_at": r.created_at.isoformat(timespec="seconds"),
    } for r in rows]}


@router.post("/{log_id}/undo")
def undo_log(log_id: int, db: Session = Depends(get_db)):
    """按日志定位文档，整链撤销（保证文件位置一致性，比单条回滚更稳）。"""
    row = db.get(OperationLog, log_id)
    if not row:
        raise HTTPException(404, "日志不存在")
    if not row.document_id:
        raise HTTPException(400, "该日志未关联文档，无法撤销")
    doc = db.get(Document, row.document_id)
    if not doc:
        raise HTTPException(400, "文档已不存在")
    try:
        steps = undo_document(db, doc)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "steps": steps, "document_id": doc.id}