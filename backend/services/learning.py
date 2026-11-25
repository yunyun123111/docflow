"""学习闭环：记录审核纠错 → 生成候选规则建议 → 一键采纳（默认停用，人工启用）。"""
from __future__ import annotations

import re
from collections import defaultdict

from sqlalchemy.orm import Session

from backend.models import Category, ClassifyRule, Document, LearnCorrection
from backend.utils.logger import get_logger

logger = get_logger("learning")

_SPLIT = re.compile(r"[、，,．.\s/（）()【】\[\]_-]+")

# 单据名后缀：抬头里最后一段就是这份文件「是什么」，比文件名可靠得多
_DOC_TAIL_RE = re.compile(
    r"[\u4e00-\u9fa5A-Za-z]{0,6}(?:结算单|提货单|入库单|出库单|出货单|报价单|对账单|回单|发票|合同|协议|凭证|订单|签收单|确认单)")


def _keyword_hint(db: Session, doc: Document) -> str:
    """候选规则的关键词：先取正文抬头的单据名，再退回文件名首段。

    扫描件的文件名常常就是一串哈希（7df284fe…），拿它当关键词，纠错建议就永远是废的——
    用户改完类型以为学会了，下一份照样识别成发票。
    """
    from backend.models import DocumentText
    from backend.services.rules import header_span

    row = db.get(DocumentText, doc.id) if doc.id else None
    head = header_span(row.text if row else "")[0]
    m = _DOC_TAIL_RE.search(head)
    if m:
        return m.group(0)[-5:]
    name = (doc.original_filename or "").rsplit(".", 1)[0]
    toks = [t for t in _SPLIT.split(name) if len(t) >= 2]
    return toks[0] if toks else ""


def record_correction(db: Session, doc: Document, from_cat: int | None, to_cat: int | None) -> None:
    if to_cat is None or from_cat == to_cat:
        return
    db.add(LearnCorrection(document_id=doc.id, from_category_id=from_cat, to_category_id=to_cat,
                           document_type=doc.document_type or "", keyword_hint=_keyword_hint(db, doc)))
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
                        field="any", category_id=cand["category_id"],
                        document_type=cand.get("document_type", ""),
                        confidence=float(cand.get("confidence", 0.6)), enabled=False)
    db.add(rule)
    db.commit()
    logger.info("候选规则已生成（未启用）: %s", rule.name)
    return rule