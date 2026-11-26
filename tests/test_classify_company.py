# -*- coding: utf-8 -*-
"""公司字段防污染：AI 只会把第一家公司塞进「公司名称」，不得覆盖本地按 OUR_COMPANY 判定的我方/对方。"""
from __future__ import annotations

import json

from backend.config import settings
from backend.services import ai
from backend.services.classifier import _merge_ai_fields
from backend.services.fields import FIELD_COMPANY, FIELD_COUNTERPARTY


def test_classify_prompt_carries_our_company_and_counterparty(monkeypatch):
    seen = {}

    def fake_chat(prompt, max_tokens=0):
        seen["prompt"] = prompt
        return json.dumps({"confidence": 0.9}, ensure_ascii=False)

    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "AI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setattr(settings, "AI_MODEL", "m")
    monkeypatch.setattr(settings, "OUR_COMPANY", "华山云创科技有限公司")
    monkeypatch.setattr(ai, "_chat", fake_chat)
    ai.classify("合同正文", "c.pdf")
    assert "华山云创科技有限公司" in seen["prompt"]
    assert FIELD_COUNTERPARTY in seen["prompt"]          # 让模型显式输出对方单位


def test_classify_prompt_marks_company_unset(monkeypatch):
    seen = {}

    def fake_chat(prompt, max_tokens=0):
        seen["prompt"] = prompt
        return json.dumps({"confidence": 0.9}, ensure_ascii=False)

    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "AI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setattr(settings, "AI_MODEL", "m")
    monkeypatch.setattr(settings, "OUR_COMPANY", "")
    monkeypatch.setattr(ai, "_chat", fake_chat)
    ai.classify("合同正文", "c.pdf")
    assert "未设置" in seen["prompt"]


def test_merge_keeps_local_company_and_never_overwrites():
    local = {FIELD_COMPANY: ("华山云创科技有限公司", 0.7), FIELD_COUNTERPARTY: ("乙公司", 0.7)}
    ai_fields = {FIELD_COMPANY: "乙公司", "日期": "2026-01-01"}
    out = _merge_ai_fields(local, ai_fields)
    assert FIELD_COMPANY not in out                      # 不覆盖本地公司
    assert FIELD_COUNTERPARTY not in out                 # 不覆盖本地对方
    assert out["日期"] == "2026-01-01"


def test_merge_company_only_when_it_matches_our_company(monkeypatch):
    monkeypatch.setattr(settings, "OUR_COMPANY", "华山云创科技（山西）有限公司")
    # 全/半角括号差异应被 _same_company 归一后视为同一家
    out = _merge_ai_fields({}, {FIELD_COMPANY: "华山云创科技(山西)有限公司"})
    assert FIELD_COMPANY in out
    # 模型把对方公司塞进公司名称 → 不是本公司，必须丢弃
    out2 = _merge_ai_fields({}, {FIELD_COMPANY: "太原乙方贸易有限公司"})
    assert FIELD_COMPANY not in out2


def test_merge_counterparty_rejected_when_it_is_our_company(monkeypatch):
    monkeypatch.setattr(settings, "OUR_COMPANY", "华山云创科技有限公司")
    out = _merge_ai_fields({}, {FIELD_COUNTERPARTY: "华山云创科技有限公司"})
    assert FIELD_COUNTERPARTY not in out                 # 对方填成自己 → 明显反了
    out2 = _merge_ai_fields({}, {FIELD_COUNTERPARTY: "太原乙方贸易有限公司"})
    assert out2.get(FIELD_COUNTERPARTY) == "太原乙方贸易有限公司"


def test_merge_drops_company_when_our_company_unset(monkeypatch):
    monkeypatch.setattr(settings, "OUR_COMPANY", "")
    out = _merge_ai_fields({}, {FIELD_COMPANY: "随便哪家公司"})
    assert FIELD_COMPANY not in out
