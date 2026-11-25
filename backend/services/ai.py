"""AI Provider：OpenAI 兼容 chat 接口，强约束 JSON + 校验 + 重试 + 截断。

仅在规则置信度不足时兜底调用；结果按文件 hash 缓存（同类重处理零成本）。
"""
from __future__ import annotations

import json
import re

from backend.config import settings
from backend.utils.logger import get_logger

logger = get_logger("ai")

CLASSIFY_PROMPT = """你是文档分类助手。阅读文档内容，输出 JSON（只输出 JSON）：
{"document_type": "类型(合同/发票/结算单/订单/报价单/出货单/银行回单/其他)",
 "category": "建议归档分类名",
 "confidence": 0到1,
 "fields": {"公司名称": "", "合同编号": "", "日期": "YYYY-MM-DD", "金额": "数字"},
 "reason": "一句话理由"}
文档内容（可能截断）：
"""

_json_re = re.compile(r"\{.*\}", re.S)


def ai_enabled() -> bool:
    return bool(settings.AI_ENABLED and settings.AI_BASE_URL and (settings.AI_MODEL or "").strip())


def _chat(prompt: str) -> str:
    import httpx

    url = settings.AI_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if settings.AI_API_KEY:
        headers["Authorization"] = f"Bearer {settings.AI_API_KEY}"
    payload = {
        "model": settings.AI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
    }
    with httpx.Client(timeout=settings.AI_TIMEOUT) as client:
        r = client.post(url, json=payload, headers=headers)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def classify(text: str, filename: str) -> dict | None:
    """返回校验后的 dict；失败返回 None（调用方降级走人工审核）。"""
    if not ai_enabled():
        return None
    snippet = text[: settings.AI_MAX_TEXT_LENGTH]
    prompt = f"{CLASSIFY_PROMPT}\n文件名：{filename}\n---\n{snippet}"
    for attempt in range(1 + max(0, settings.AI_MAX_RETRIES)):
        try:
            raw = _chat(prompt)
            m = _json_re.search(raw)
            if not m:
                raise ValueError("响应不含 JSON")
            data = json.loads(m.group(0))
            conf = float(data.get("confidence", 0) or 0)
            if not 0 <= conf <= 1:
                raise ValueError("confidence 越界")
            data["confidence"] = conf
            fields = data.get("fields") or {}
            data["fields"] = {str(k): str(v) for k, v in fields.items() if v}
            return data
        except Exception as e:  # noqa: BLE001
            logger.warning("AI 调用失败(第 %s 次): %s", attempt + 1, e)
    return None