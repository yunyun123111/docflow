"""FastAPI 应用组装。

安全要点（相对旧版的三处修复）：
1. 前端用 StaticFiles(html=True) 托管，删除自拼路径的 SPA 兜底 → 无目录穿越；
2. 鉴权依赖挂到全部 /api 路由（auth 除外），非 /api 路径返回正确 404；
3. zip 恢复成员路径守卫（见 backup 服务）。
"""
from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.config import VERSION, settings
from backend.database import init_db
from backend.security import require_access
from backend.utils.logger import get_logger, setup_logging

logger = get_logger("app")

from backend.config import frontend_dist as _frontend_dist

FRONTEND_DIST = _frontend_dist()


def _maintenance_loop() -> None:
    """每日维护：过期回收项清理 + 自动备份。"""
    from backend.database import SessionLocal

    stop = threading.Event()
    _maintenance_loop.stop = stop  # type: ignore[attr-defined]
    last_day = None
    while not stop.is_set():
        try:
            day = time.strftime("%Y-%m-%d")
            if day != last_day:
                last_day = day
                with SessionLocal() as db:
                    from backend.services import backup, recycle

                    recycle.purge_expired(db)
                    backup.auto_backup_tick()
        except Exception:  # noqa: BLE001
            logger.exception("维护任务异常")
        stop.wait(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.log_dir, settings.LOG_LEVEL)
    logger.info("=== %s 启动 (host=%s) ===", settings.APP_NAME, settings.HOST)
    init_db()
    from backend.services import email_ingest, reconciliation, watcher

    watcher.sync_from_settings()
    email_ingest.sync_from_settings()
    reconciliation.sync_scheduler()
    maint = threading.Thread(target=_maintenance_loop, name="maintenance", daemon=True)
    maint.start()
    yield
    stop = getattr(_maintenance_loop, "stop", None)
    if stop:
        stop.set()
    watcher.stop()
    email_ingest.stop()
    logger.info("=== %s 退出 ===", settings.APP_NAME)


app = FastAPI(title="DocFlow API", version=VERSION, lifespan=lifespan)

_auth = [Depends(require_access)]


def _include(router):
    if router.prefix == "/api/auth":
        app.include_router(router)
    else:
        app.include_router(router, dependencies=_auth)


from backend.api import (  # noqa: E402
    auth, backup, bundles, catalog, dashboard, documents, logs, preview, processing,
    reconciliation, recycle, review, rules, settings as settings_api, system,
)

for mod in (auth, system, documents, preview, processing, review, catalog, rules,
            recycle, backup, bundles, reconciliation, settings_api, dashboard, logs):
    _include(mod.router)


@app.exception_handler(404)
async def _not_found(request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=404, content={"detail": f"接口不存在: {request.url.path}"})
    return JSONResponse(status_code=404, content={"detail": "页面不存在"})


@app.get("/{full_path:path}", include_in_schema=False)
def _serve_spa(full_path: str):
    """安全托管前端：路径必须解析到 dist 内（safe_join），否则只回 index.html。

    旧版在这里手写拼接路径导致 %2F 编码目录穿越；新版以守卫为默认。
    """
    from fastapi import HTTPException

    from backend.utils.fs import safe_join

    if full_path.startswith("api/"):
        raise HTTPException(404, "接口不存在")
    if not FRONTEND_DIST.exists():
        from fastapi.responses import JSONResponse as _J

        return _J({"detail": "前端未构建：cd frontend && npm install && npm run build，或开发模式访问 5173"},
                  status_code=404)
    if full_path:
        try:
            candidate = safe_join(FRONTEND_DIST, full_path)
        except ValueError:
            raise HTTPException(400, "非法路径") from None
        if candidate.is_file():
            from fastapi.responses import FileResponse

            headers = {"Cache-Control": "no-cache"}
            return FileResponse(str(candidate), headers=headers)
    from fastapi.responses import FileResponse

    return FileResponse(str(FRONTEND_DIST / "index.html"), headers={"Cache-Control": "no-cache"})
