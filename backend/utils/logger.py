"""日志：控制台 + 滚动文件 + 文件操作审计流水。"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

_configured = False


def setup_logging(log_dir, level: str = "INFO") -> None:
    global _configured
    if _configured:
        return
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    try:
        fh = RotatingFileHandler(str(log_dir / "app.log"), maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)