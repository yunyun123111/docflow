"""AI Provider 约束：推理/中转站点单次常要 30~90s，自检必须限流、报错必须可读、慢超时不得叠加重试。"""
from __future__ import annotations

import httpx
import pytest

from backend.config import settings
from backend.services import ai


def _configured(monkeypatch):
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "AI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setattr(settings, "AI_MODEL", "deepseek-test")


def test_ping_limits_output_tokens(monkeypatch):
    """自检只验证连通性，不限 max_tokens 时一句 OK 也能跑几十秒。"""
    seen = {}

    def fake_chat(prompt, max_tokens=0):
        seen["max_tokens"] = max_tokens
        return " OK"

    monkeypatch.setattr(ai, "_chat", fake_chat)
    echo, cost = ai.ping()
    assert echo == "OK"
    assert cost >= 0
    assert seen["max_tokens"] > 0


def test_chat_timeout_message_is_actionable(monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setattr(settings, "AI_TIMEOUT", 1)

    class Boom(httpx.Client):
        def post(self, *args, **kwargs):
            raise httpx.ReadTimeout("boom")

    monkeypatch.setattr(httpx, "Client", Boom)
    with pytest.raises(ai.AiError) as excinfo:
        ai._chat("hi")
    assert "超时" in str(excinfo.value)
    assert excinfo.value.retryable is False


def test_classify_does_not_retry_timeout(monkeypatch):
    calls: list[int] = []

    def fake_chat(prompt, max_tokens=0):
        calls.append(1)
        raise ai.AiError("请求超时（120s）", retryable=False)

    _configured(monkeypatch)
    monkeypatch.setattr(settings, "AI_MAX_RETRIES", 2)
    monkeypatch.setattr(ai, "_chat", fake_chat)
    assert ai.classify("内容", "a.pdf") is None
    assert len(calls) == 1


def test_classify_retries_unparsable_answer(monkeypatch):
    calls: list[int] = []

    def fake_chat(prompt, max_tokens=0):
        calls.append(1)
        return "抱歉，我无法输出 JSON"

    _configured(monkeypatch)
    monkeypatch.setattr(settings, "AI_MAX_RETRIES", 2)
    monkeypatch.setattr(ai, "_chat", fake_chat)
    assert ai.classify("内容", "a.pdf") is None
    assert len(calls) == 3


def test_settings_test_ai_reports_elapsed(client, monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setattr(ai, "ping", lambda: ("OK", 12.3))
    body = client.post("/api/settings/test/ai").json()
    assert body["ok"] is True
    assert body["seconds"] == 12.3
    assert "12.3" in body["detail"]


def test_settings_test_ai_disabled_needs_no_network(client, monkeypatch):
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    body = client.post("/api/settings/test/ai").json()
    assert body["ok"] is False
    assert "未启用" in body["detail"]



class _FakeResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}]}


def _capture_chat(monkeypatch):
    seen = {}

    class FakeClient(httpx.Client):
        def post(self, url, json=None, headers=None):
            seen["payload"] = json
            return _FakeResp()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    return seen


def test_chat_sends_disable_thinking_params(monkeypatch):
    """关掉思考是提速省钱的关键：必须把厂商两种参数名都发出去。"""
    _configured(monkeypatch)
    seen = _capture_chat(monkeypatch)
    monkeypatch.setattr(settings, "AI_DISABLE_THINKING", True)
    assert ai._chat("hi") == "ok"
    assert seen["payload"].get("thinking") == {"type": "disabled"}
    assert seen["payload"].get("enable_thinking") is False


def test_chat_keeps_thinking_when_flag_off(monkeypatch):
    """开关关闭时不得污染请求体，避免某些严格网关因未知键报 4xx。"""
    _configured(monkeypatch)
    seen = _capture_chat(monkeypatch)
    monkeypatch.setattr(settings, "AI_DISABLE_THINKING", False)
    ai._chat("hi")
    assert "thinking" not in seen["payload"]
    assert "enable_thinking" not in seen["payload"]
