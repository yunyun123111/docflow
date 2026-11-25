"""文本归一化：给关键词/样例匹配用的轻量清洗。"""
from __future__ import annotations

import re

_ws = re.compile(r"\s+")


def normalize_for_match(text: str) -> str:
    return _ws.sub("", (text or "").lower())