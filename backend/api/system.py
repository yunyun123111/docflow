"""系统状态：健康检查、运行形态信息、配置自检、一键修复、后台组件状态。

桌面版（DocFlow.exe）与源码版共用同一套接口，前端顶栏据此显示
「运行形态 / 自检徽标 / 后台组件」，设置页据此做配置状态自检面板。
"""
from __future__ import annotations

import os
import platform
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from backend.config import runtime_info, settings
from backend.database import get_db
from backend.services import email_ingest, selfcheck, watcher
from backend.services.pipeline import pipeline

router = APIRouter(prefix="/api/system", tags=["system"])
_STARTED = time.time()


@router.get("/health")
def health():
    return {"ok": True, "app": settings.APP_NAME, "host": settings.HOST,
            "password_required": bool(settings.ACCESS_PASSWORD),
            "ai_enabled": settings.AI_ENABLED, "ocr_enabled": settings.OCR_ENABLED,
            "watching": pipeline.has_running()}


@router.get("/info")
def info(request: Request):
    """运行形态与位置：桌面版 exe / 源码、数据目录、资源目录、监听地址。"""
    ri = runtime_info()
    ri.update({
        "started_at": datetime.fromtimestamp(_STARTED).isoformat(timespec="seconds"),
        "uptime_seconds": int(time.time() - _STARTED),
        "base_url": str(request.base_url).rstrip("/"),
        "host": settings.HOST,
        "desktop_shell": os.environ.get("DOCFLOW_DESKTOP") == "1",
        "os": f"{platform.system()} {platform.release()}",
        "data_free_mb": int(__import__("shutil").disk_usage(str(runtime_info()["data_dir"])).free / 2**20),
    })
    return ri


@router.get("/selfcheck")
def selfcheck_all(quick: bool = False):
    """配置状态自检：运行形态/目录/磁盘/数据库/FTS/缓存/队列/OCR/AI/监控/邮箱/安全/备份。"""
    return selfcheck.run_checks(quick=quick)


@router.get("/status")
def status():
    """轻量自检（结果缓存 60 秒），供顶栏徽标高频轮询。"""
    return selfcheck.quick_status()


@router.post("/repair/{action}")
def repair(action: str):
    if action not in selfcheck.FIXES:
        raise HTTPException(400, f"仅支持: {', '.join(selfcheck.FIXES)}")
    return selfcheck.repair(action)


@router.get("/components")
def components(db: Session = Depends(get_db)):
    from backend.services.reconciliation import _status as recon_status

    from backend.models import ProcessingJob

    job = (db.query(ProcessingJob).filter(ProcessingJob.status == "running")
           .order_by(ProcessingJob.id.desc()).first())
    return {
        "watcher": watcher.get_status(),
        "email": email_ingest.get_status(),
        "recon": recon_status,
        "pipeline_running": pipeline.has_running(),
        "active_job": ({"id": job.id, "kind": job.kind, "processed": job.processed,
                        "total": job.total, "failed": job.failed, "message": (job.message or "")[:120]}
                       if job else None),
    }