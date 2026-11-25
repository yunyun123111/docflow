"""看板 API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.services import dashboard

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    return dashboard.overview(db)


@router.get("/trend")
def trend(days: int = 30, db: Session = Depends(get_db)):
    return dashboard.trend(db, days)


@router.get("/quality")
def quality(db: Session = Depends(get_db)):
    return dashboard.quality(db)


@router.get("/storage")
def storage(db: Session = Depends(get_db)):
    return dashboard.storage(db)


@router.get("/recent-operations")
def recent(limit: int = 20, db: Session = Depends(get_db)):
    return dashboard.recent_operations(db, limit)