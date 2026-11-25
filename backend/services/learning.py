"""学习闭环：记录审核纠错 → 生成候选规则建议 → 一键采纳（默认停用，人工启用）。"""
from __future__ import annotations

import re
from collections import defaultdict

from sqlalchemy.orm import Session

from backend.models import Category, ClassifyRule, Document, LearnCorrection
from backend.utils.logger import get_logger

logger = get_logger("learning")

_SPLIT = re.compile(r"[、，,．.\s/（）()【】\[\]_-]+")


def _keyword_hint(doc: Document) -> str:
    name = (doc.original_filename or "").rsplit(".", 1)[0]
    toks = [t for t in _SPLIT.split(name) if len(t) >= 2]
    return toks[0] if toks else ""


def record_correction(db: Session, doc: Document, from_cat: int | None, to_cat: int | None) -> None:
    if to_cat is None or from_cat == to_cat:
        return
    db.add(LearnCorrection(document_id=doc.id, from_category_id=from_cat, to_category_id=to_cat,
                           document_type=doc.document_type or "", keyword_hint=_keyword_hint(doc)))
    db.commit()


def suggestions(db: Session, min_count: int = 2) -> list[dict]:
    stat: dict[tuple, int] = defaultdict(int)
    for c in db.query(LearnCorrection).filter(LearnCorrection.keyword_hint != "").all():
        if c.to_category_id:
            stat[(c.keyword_hint, c.to_category_id, c.document_type)] += 1
    out = []
    for (kw, cat_id, dtype), n in sorted(stat.items(), key=lambda kv: -kv[1]):
        if n < min_count:
            continue
        cat = db.get(Category, cat_id)
        out.append({
            "keyword": kw, "category_id": cat_id,
            "category_path": cat.path if cat else "", "document_type": dtype,
            "count": n, "confidence": round(min(0.9, 0.5 + 0.1 * n), 2),
        })
    return out[:30]


def adopt(db: Session, cand: dict) -> ClassifyRule:
    rule = ClassifyRule(name=f"学习:{cand['keyword']}", pattern=cand["keyword"], match_type="keyword",
                        field="filename", category_id=cand["category_id"],
                        document_type=cand.get("document_type", ""),
                        confidence=float(cand.get("confidence", 0.6)), enabled=False)
    db.add(rule)
    db.commit()
    logger.info("候选规则已生成（未启用）: %s", rule.name)
    return rule