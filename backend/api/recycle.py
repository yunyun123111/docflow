"""回收站 API：列表 / 恢复 / 彻底删除（需显式确认）/ 清空。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import RecycleItem
from backend.schemas import BatchIds
from backend.services import recycle as svc
from backend.services.recycle import RecycleError

router = APIRouter(prefix="/api/recycle", tags=["recycle"])


def _out(i: RecycleItem) -> dict:
    from backend.services.parser import get_file_type

    return {"id": i.id, "document_id": i.document_id, "name": i.name, "reason": i.reason,
             "file_type": get_file_type(Path(i.name)) if i.name else "",
             "original_path": i.original_path, "file_missing": svc.item_file_missing(i),
             "prev_status": i.prev_status or "",
             "created_at": i.created_at.isoformat(timespec="seconds"),
             "expires_at": i.expires_at.isoformat(timespec="seconds") if i.expires_at else None}


@router.get("")
def listing(db: Session = Depends(get_db)):
    return [_out(i) for i in db.query(RecycleItem).order_by(RecycleItem.id.desc()).all()]


@router.post("/{item_id}/restore")
def restore(item_id: int, db: Session = Depends(get_db)):
    item = db.get(RecycleItem, item_id)
    if not item:
        raise HTTPException(404, "回收项不存在")
    try:
        doc = svc.restore(db, item)
    except RecycleError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "document_id": doc.id if doc else None}


@router.post("/{item_id}/purge")
def purge(item_id: int, confirm: bool = False, db: Session = Depends(get_db)):
    if not confirm:
        raise HTTPException(400, "彻底删除需要 confirm=true")
    item = db.get(RecycleItem, item_id)
    if not item:
        raise HTTPException(404, "回收项不存在")
    svc.purge(db, item)
    return {"ok": True}


@router.post("/empty")
def empty(confirm: bool = False, db: Session = Depends(get_db)):
    if not confirm:
        raise HTTPException(400, "清空回收站需要 confirm=true")
    n = 0
    for item in db.query(RecycleItem).all():
        try:
            svc.purge(db, item)
            n += 1
        except Exception:  # noqa: BLE001
            db.rollback()
    return {"purged": n}