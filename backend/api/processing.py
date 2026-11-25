"""整理任务 API。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import get_db
from backend.models import ProcessingJob
from backend.schemas import ProcessRequest
from backend.services.pipeline import pipeline
from backend.utils.logger import get_logger

router = APIRouter(prefix="/api/processing", tags=["processing"])
logger = get_logger("api.processing")

ALLOWED_ROOTS_NOTE = "source_dir 允许任意本机目录（本地工具定位）；回收站目录除外"


def _job_out(j: ProcessingJob) -> dict:
    return {"id": j.id, "source": j.source, "kind": j.kind, "status": j.status,
            "total": j.total, "processed": j.processed, "archived": j.archived,
            "need_review": j.need_review, "duplicate": j.duplicate, "failed": j.failed,
            "message": j.message,
            "started_at": j.started_at.isoformat(timespec="seconds") if j.started_at else None,
            "ended_at": j.ended_at.isoformat(timespec="seconds") if j.ended_at else None}


@router.post("/jobs")
def start_job(req: ProcessRequest, db: Session = Depends(get_db)):
    try:
        job = pipeline.start_dir_job(db, req.source_dir, recursive=req.recursive)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    except (FileNotFoundError, ValueError, PermissionError) as e:
        raise HTTPException(400, str(e)) from e
    return _job_out(job)


@router.get("/jobs/current")
def current_job(db: Session = Depends(get_db)):
    j = db.query(ProcessingJob).filter(ProcessingJob.status == "running") \
        .order_by(ProcessingJob.id.desc()).first()
    return _job_out(j) if j else None


@router.get("/jobs")
def jobs(limit: int = 20, db: Session = Depends(get_db)):
    return [_job_out(j) for j in db.query(ProcessingJob).order_by(ProcessingJob.id.desc()).limit(limit).all()]


@router.get("/jobs/{job_id}")
def job_detail(job_id: int, db: Session = Depends(get_db)):
    j = db.get(ProcessingJob, job_id)
    if not j:
        raise HTTPException(404, "任务不存在")
    return _job_out(j)


@router.get("/jobs/{job_id}/documents")
def job_documents(job_id: int, limit: int = 200, db: Session = Depends(get_db)):
    """本次任务处理过的文档清单（前端「整理结果」直接点开预览）。"""
    from backend.api.documents import _brief
    from backend.models import Document

    if not db.get(ProcessingJob, job_id):
        raise HTTPException(404, "任务不存在")
    rows = (db.query(Document).filter(Document.job_id == job_id)
            .order_by(Document.id.desc()).limit(min(limit, 500)).all())
    return [_brief(d) for d in rows]


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int):
    ok = pipeline.cancel(job_id)
    if not ok:
        raise HTTPException(404, "任务不在运行中")
    return {"ok": True}


@router.post("/retry-failed")
def retry_failed(only: str | None = None, db: Session = Depends(get_db)):
    job = pipeline.retry_failed(db, only=only)
    if not job:
        raise HTTPException(400, "没有可重试的失败文档")
    return _job_out(job)


@router.get("/inbox/check")
def inbox_check(db: Session = Depends(get_db)):
    """待整理区体检：数量/体积/不稳定文件。"""
    inbox = settings.inbox_root
    files = [p for p in inbox.iterdir() if p.is_file()] if inbox.is_dir() else []
    unstable = [p.name for p in files if (Path(p).stat().st_size == 0)]
    return {"count": len(files),
            "bytes": sum(p.stat().st_size for p in files),
            "empty_files": unstable[:20], "note": ALLOWED_ROOTS_NOTE}