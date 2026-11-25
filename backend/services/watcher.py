"""目录自动监控：轮询快照，只处理“新出现且稳定”的文件；忙时跳过本轮。"""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from backend.config import settings
from backend.services.pipeline import _is_stable, _iter_candidates, pipeline
from backend.utils.logger import get_logger

logger = get_logger("watcher")

_thread: threading.Thread | None = None
_stop = threading.Event()
_status: dict = {"running": False, "last_cycle": None, "last_found": 0, "dirs": [], "error": ""}


def _loop(interval: int, stop: threading.Event) -> None:
    from backend.database import SessionLocal

    known: set[tuple] = set()
    while not stop.is_set():
        try:
            found = 0
            if not pipeline.has_running():
                with SessionLocal() as db:
                    files: list[Path] = []
                    for d in settings.watch_dirs:
                        if not d.is_dir():
                            continue
                        for p in _iter_candidates(d, recursive=True):
                            if not _is_stable(p):
                                continue
                            st = p.stat()
                            key = (str(p), st.st_size, int(st.st_mtime))
                            if key in known:
                                continue
                            known.add(key)
                            files.append(p)
                    files = pipeline._drop_known(db, files)
                    found = len(files)
                    if files:
                        pipeline.start_files_job(db, files, kind="watch",
                                                 source=";".join(str(d) for d in settings.watch_dirs))
                _status.update(last_cycle=datetime.now().isoformat(timespec="seconds"),
                               last_found=found, error="")
        except Exception as e:  # noqa: BLE001
            logger.exception("监控轮询失败")
            _status["error"] = str(e)
        stop.wait(interval)
    _status["running"] = False


def start() -> bool:
    global _thread, _stop
    if _thread and _thread.is_alive():
        return True
    _stop = threading.Event()
    _thread = threading.Thread(target=_loop, args=(settings.WATCH_INTERVAL, _stop),
                               name="watcher", daemon=True)
    _status.update(running=True, dirs=[str(d) for d in settings.watch_dirs])
    _thread.start()
    return True


def stop() -> None:
    _stop.set()


def sync_from_settings() -> bool:
    if settings.WATCH_ENABLED and settings.watch_dirs:
        return start()
    stop()
    return False


def get_status() -> dict:
    return {**_status, "enabled": settings.WATCH_ENABLED,
            "interval": settings.WATCH_INTERVAL,
            "dirs": [str(d) for d in settings.watch_dirs]}