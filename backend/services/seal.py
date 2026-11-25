"""盖章检测：HSV 红色像素占比 + 连通域尺寸，本地算法零 token。"""
from __future__ import annotations

from pathlib import Path


def detect_seal(image_bytes: bytes) -> bool:
    try:
        import cv2
        import numpy as np
    except ImportError:
        return False
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return False
    h, w = img.shape[:2]
    if h * w > 4_000_000:
        scale = (2000 / max(h, w))
        img = cv2.resize(img, (int(w * scale), int(h * scale)))
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    red = cv2.inRange(hsv, (0, 70, 60), (10, 255, 255)) | cv2.inRange(hsv, (170, 70, 60), (180, 255, 255))
    ratio = float(red.sum()) / 255 / red.size
    if ratio < 0.0008:
        return False
    n, _, stats, _ = cv2.connectedComponentsWithStats((red > 0).astype("uint8"), 8)
    for i in range(1, n):
        bw, bh = stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT]
        if 40 <= bw <= 400 and 40 <= bh <= 400 and 0.6 <= bw / max(bh, 1) <= 1.7:
            return True
    return False


def check(path: Path, file_type: str) -> bool | None:
    from backend.config import settings

    if not settings.SEAL_DETECT_ENABLED:
        return None
    try:
        if file_type == "image":
            return detect_seal(path.read_bytes())
        if file_type == "pdf":
            import fitz

            with fitz.open(str(path)) as doc:
                for page in list(doc)[:3]:
                    pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
                    if detect_seal(pix.tobytes("png")):
                        return True
            return False
    except Exception:  # noqa: BLE001
        return None
    return None