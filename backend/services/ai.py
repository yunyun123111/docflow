"""AI Provider：OpenAI 兼容 chat 接口，强约束 JSON + 校验 + 重试 + 截断。

仅在规则置信度不足时兜底调用；结果按文件 hash 缓存（同类重处理零成本）。
"""
from __future__ import annotations

import json
import re
import time

from backend.config import settings
from backend.utils.logger import get_logger

logger = get_logger("ai")

CLASSIFY_PROMPT = """你是文档分类助手。阅读文档内容，输出 JSON（只输出 JSON）：
{"document_type": "类型(合同/发票/结算单/订单/报价单/出货单/银行回单/其他)",
 "category": "建议归档分类名",
 "confidence": 0到1,
 "fields": {"公司名称": "", "对方单位": "", "合同编号": "", "日期": "YYYY-MM-DD", "金额": "数字"},
 "reason": "一句话理由"}
字段规则（公司名用文中出现的规范全称，别简写、别编造）：
- 公司名称：仅填写「本公司」（见下方本公司名称），文中没有出现本公司就留空，切勿把对方或随便一家填进来。
- 对方单位：填写与本公司交易的那一方（客户/供应商/买方/卖方/付款方等）。
文档内容（可能截断）：
"""

_json_re = re.compile(r"\{.*\}", re.S)


class AiError(RuntimeError):
    """带可读诊断与「是否值得重试」标记的 AI 调用异常。"""

    def __init__(self, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


def ai_enabled() -> bool:
    return bool(settings.AI_ENABLED and settings.AI_BASE_URL and (settings.AI_MODEL or "").strip())


def _timeout():
    import httpx

    # 连接阶段单独收紧：Base URL 写错或 DNS/代理不通时几秒就该报错，不必等满 AI_TIMEOUT
    return httpx.Timeout(float(settings.AI_TIMEOUT), connect=min(15.0, float(settings.AI_TIMEOUT)))


def _describe_error(exc: Exception) -> str:
    import httpx

    if isinstance(exc, httpx.TimeoutException):
        return (f"请求超时（{settings.AI_TIMEOUT}s）：模型或中转服务响应过慢，"
                f"可调大「超时（秒）」或改用非推理类模型（推理模型单次要 30~90s）")
    if isinstance(exc, httpx.ConnectError):
        return f"连不上 {settings.AI_BASE_URL}：检查 Base URL、网络或系统代理"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}：{(exc.response.text or '')[:120]}"
    return f"{type(exc).__name__}: {exc}"


def _retryable(exc: Exception) -> bool:
    import httpx

    if isinstance(exc, httpx.TimeoutException):
        return False          # 慢链路重试只会把等待时间再叠一层
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return True


def _chat(prompt: str, max_tokens: int = 0) -> str:
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
    if settings.AI_DISABLE_THINKING:
        # 厂商参数名不一：DeepSeek/智谱用 thinking，阿里百炼等用 enable_thinking；未知键一般被网关忽略
        payload["thinking"] = {"type": "disabled"}
        payload["enable_thinking"] = False
    if max_tokens > 0:
        payload["max_tokens"] = max_tokens
    try:
        with httpx.Client(timeout=_timeout()) as client:
            r = client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
            # 推理模型可能把预算全花在思考上，正文为空时退回思考内容
            return str(msg.get("content") or msg.get("reasoning_content") or "")
    except Exception as e:  # noqa: BLE001
        raise AiError(_describe_error(e), retryable=_retryable(e)) from e


def ping() -> tuple[str, float]:
    """连通性自检：限死输出长度，否则回一句 OK 也要跑几十秒。返回（回复, 耗时秒）。"""
    t0 = time.perf_counter()
    reply = _chat("只回复 OK 两个字母，不要解释", max_tokens=64)
    return reply.strip()[:40], round(time.perf_counter() - t0, 1)


def classify(text: str, filename: str) -> dict | None:
    """返回校验后的 dict；失败返回 None（调用方降级走人工审核）。"""
    if not ai_enabled():
        return None
    snippet = text[: settings.AI_MAX_TEXT_LENGTH]
    ours = (settings.OUR_COMPANY or "").strip()
    head = f"本公司名称：{ours}\n" if ours else "本公司名称：（未设置，公司名称一律留空）\n"
    prompt = f"{CLASSIFY_PROMPT}{head}\n文件名：{filename}\n---\n{snippet}"
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
            if not getattr(e, "retryable", True):
                break
    return None
