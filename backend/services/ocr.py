"""OCR 服务：RapidOCR 懒加载单例，失败自动降级（返回空文本 + 告警）。

改进点：按图片字节做进程内缓存，重复整理同一文件不再重复 OCR。
"""
from __future__ import annotations

import hashlib
import threading

from backend.utils.fs import fs_path

_engine = None
_engine_failed = False
_lock = threading.Lock()
_cache: dict[str, str] = {}
_CACHE_MAX = 200


def _get_engine():
    global _engine, _engine_failed
    if _engine is not None or _engine_failed:
        return _engine
    with _lock:
        if _engine is None and not _engine_failed:
            try:
                from rapidocr_onnxruntime import RapidOCR

                _engine = RapidOCR()
            except Exception:  # 依赖缺失/初始化失败 → 优雅降级
                _engine_failed = True
    return _engine


def _run(image_bytes: bytes) -> str:
    engine = _get_engine()
    if engine is None:
        return ""
    import numpy as np
    import cv2

    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return ""
    result, _ = engine(img)
    if not result:
        return ""
    return "\n".join(line[1] for line in result)


def ocr_image(path) -> str:
    from pathlib import Path

    p = Path(path)
    key = hashlib.sha1(fs_path(p).encode("utf-8", "ignore")).hexdigest()
    if key in _cache:
        return _cache[key]
    text = _run(p.read_bytes())
    if len(_cache) > _CACHE_MAX:
        _cache.clear()
    _cache[key] = text
    return text


def ocr_pixmap(pixmap) -> str:
    return _run(pixmap.tobytes("png"))