"""置信度合成：规则40% + 字段20% + 关键词15% + AI25%（权重可配置）。"""
from __future__ import annotations

from backend.config import settings


def combine(rule_score: float, field_score: float, keyword_score: float, ai_score: float) -> tuple[float, dict]:
    w = settings.confidence_weights()
    total = rule_score * w["rule"] + field_score * w["field"] + keyword_score * w["keyword"] + ai_score * w["ai"]
    detail = {"rule": rule_score, "field": field_score, "keyword": keyword_score, "ai": ai_score, "weights": w}
    return round(min(max(total, 0.0), 1.0), 4), detail


def field_coverage(values: dict) -> float:
    """四大关键字段的覆盖率 × 平均可信度。"""
    from backend.services.fields import FIELD_AMOUNT, FIELD_COMPANY, FIELD_CONTRACT_NO, FIELD_DATE

    keys = [FIELD_DATE, FIELD_CONTRACT_NO, FIELD_AMOUNT, FIELD_COMPANY]
    got = [values[k] for k in keys if k in values]
    if not got:
        return 0.0
    avg_conf = sum(c for _, c in got) / len(got)
    return len(got) / len(keys) * avg_conf