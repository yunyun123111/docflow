"""看板：把“机器处理确定、人处理不确定”量化成可决策的指标。"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import Document, OperationLog, ProcessingJob, STATUS_ARCHIVED, STATUS_FAILED, STATUS_NEED_REVIEW, STATUS_PENDING


def overview(db: Session) -> dict:
    total = db.query(func.count(Document.id)).scalar() or 0
    status_counts = dict(db.query(Document.status, func.count(Document.id)).group_by(Document.status).all())
    today = datetime.now().strftime("%Y-%m-%d")
    today_new = db.query(func.count(Document.id)).filter(func.date(Document.created_at) == today).scalar() or 0
    today_arch = db.query(func.count(Document.id)).filter(func.date(Document.archived_at) == today).scalar() or 0
    running = db.query(ProcessingJob).filter(ProcessingJob.status == "running").order_by(ProcessingJob.id.desc()).first()
    return {
        "total": total,
        "by_status": status_counts,
        "pending": status_counts.get(STATUS_PENDING, 0),
        "need_review": status_counts.get(STATUS_NEED_REVIEW, 0),
        "archived": status_counts.get(STATUS_ARCHIVED, 0),
        "failed": status_counts.get(STATUS_FAILED, 0),
        "today_new": today_new,
        "today_archived": today_arch,
        "running_job": None if not running else {
            "id": running.id, "total": running.total, "processed": running.processed,
            "kind": running.kind, "source": running.source,
        },
        "document_root": str(settings.document_root),
        "inbox_root": str(settings.inbox_root),
    }


def trend(db: Session, days: int = 30) -> list[dict]:
    days = max(1, min(days, 180))
    start = datetime.now().date() - timedelta(days=days - 1)
    out = []
    created = db.query(func.date(Document.created_at).label("d"), func.count(Document.id)) \
        .filter(func.date(Document.created_at) >= start.isoformat()).group_by("d").all()
    archived = db.query(func.date(Document.archived_at).label("d"), func.count(Document.id)) \
        .filter(func.date(Document.archived_at) >= start.isoformat()).group_by("d").all()
    failed = db.query(func.date(Document.processed_at).label("d"), func.count(Document.id)) \
        .filter(Document.status == STATUS_FAILED, func.date(Document.processed_at) >= start.isoformat()) \
        .group_by("d").all()
    cm, am, fm = dict(created), dict(archived), dict(failed)
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        out.append({"date": d, "new": cm.get(d, 0), "archived": am.get(d, 0), "failed": fm.get(d, 0)})
    return out


def quality(db: Session) -> dict:
    """置信度分布 + 自动化率 + 审核积压年龄。"""
    buckets = [0.0] * 10
    scored = db.query(Document.confidence).filter(Document.confidence.isnot(None)).all()
    for (c,) in scored:
        buckets[min(int(c * 10), 9)] += 1
    since = datetime.now() - timedelta(days=30)
    handled = db.query(
        func.sum(case((Document.status == STATUS_ARCHIVED, 1), else_=0)).label("archived"),
        func.sum(case((Document.status == STATUS_NEED_REVIEW, 1), else_=0)).label("review"),
    ).filter(Document.processed_at >= since).first()
    a, r = int(handled.archived or 0), int(handled.review or 0)
    auto_ratio = round(a / (a + r), 4) if (a + r) else None
    backlog = db.query(
        func.sum(case((Document.processed_at < since - timedelta(days=7), 1), else_=0)).label("stale"),
        func.count(Document.id).label("total"),
    ).filter(Document.status == STATUS_NEED_REVIEW).first()
    return {
        "confidence_buckets": buckets,
        "auto_archive_ratio_30d": auto_ratio,
        "archived_30d": a,
        "review_30d": r,
        "backlog_total": int(backlog.total or 0),
        "backlog_over_7d": int(backlog.stale or 0),
    }


def storage(db: Session) -> dict:
    from pathlib import Path

    def dsize(p: Path) -> int:
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else 0

    return {
        "archive_bytes": dsize(settings.document_root),
        "inbox_count": sum(1 for f in settings.inbox_root.iterdir() if f.is_file()) if settings.inbox_root.is_dir() else 0,
        "recycle_bytes": dsize(settings.recycle_root),
    }


def recent_operations(db: Session, limit: int = 20) -> list[dict]:
    rows = db.query(OperationLog).order_by(OperationLog.id.desc()).limit(limit).all()
    return [{"id": r.id, "op": r.op, "document_id": r.document_id, "detail": r.detail,
             "result": r.result, "created_at": r.created_at.isoformat(timespec="seconds")} for r in rows]