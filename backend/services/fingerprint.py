"""指纹：正文归一化（数字脱敏 + 取头部）后哈希，用于近似同模板文档聚类。"""
from __future__ import annotations

import hashlib
import re

_num = re.compile(r"\d+")


def fingerprint(text: str) -> str:
    norm = _num.sub("#", (text or "").strip())[:800]
    norm = re.sub(r"\s+", "", norm)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:32] if norm else ""