"""规则引擎：识别规则（关键词/正则 × 文件名/正文）+ 文件名清洗规则。"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from backend.models import ClassifyRule, FilenameRule

# 「合同编号 / 合同金额」这类字样是在**引用**合同，不是说这份文件本身就是合同。
# 不剥掉它们，每一份发票、结算单、提货单都会因为写着合同号而被通用「合同」规则抢走类型，
# 归集角色与必备单据检查（缺发票/缺提货单）就全都对不上。
_CONTRACT_REF_RE = re.compile(
    r"合同(?:编?号|金额|价款|总价|总额|税率|税额|期间|有效期|名称|标的|双方|性质|类别|类型)")


@dataclass
class RuleHit:
    rule: ClassifyRule
    score: float


def evidence_text(text: str) -> str:
    """关键词规则看到的正文：合同引用字样已剥离，标题性证据保留。"""
    return _CONTRACT_REF_RE.sub("", text or "")


def _match(mt: str, pattern: str, text: str) -> bool:
    if not text:
        return False
    if mt == "regex":
        try:
            return re.search(pattern, text, re.I) is not None
        except re.error:
            return False
    return pattern.lower() in text.lower()


def evaluate(db: Session, filename: str, text: str) -> list[RuleHit]:
    """按优先级返回命中规则（同分类只保留最高分一次）。

    关键词规则匹配剥离过合同引用字样的正文；正则规则仍看到原文，不改变用户自定义规则的行为。
    """
    body = evidence_text(text)
    hits: list[RuleHit] = []
    seen: set[tuple] = set()
    rules = db.query(ClassifyRule).filter(ClassifyRule.enabled.is_(True)).order_by(ClassifyRule.priority).all()
    for rule in rules:
        scope = rule.field
        candidates = {
            "filename": ((filename, filename),),
            "text": ((text, body),),
            "any": ((filename, filename), (text, body)),
        }[scope]
        for raw, probe in candidates:
            target = raw if rule.match_type == "regex" else probe
            if _match(rule.match_type, rule.pattern, target or ""):
                key = (rule.category_id, rule.document_type)
                if key not in seen:
                    seen.add(key)
                    hits.append(RuleHit(rule=rule, score=rule.confidence))
                break
    hits.sort(key=lambda h: (h.rule.priority, -h.score))
    return hits


def apply_filename_rules(db: Session, name: str) -> str:
    for fr in db.query(FilenameRule).filter(FilenameRule.enabled.is_(True)).order_by(FilenameRule.priority).all():
        try:
            name = re.sub(fr.regex, fr.replacement, name)
        except re.error:
            continue
    return name


def preview(db: Session, filename: str, text: str) -> list[dict]:
    """规则模拟器：逐条展示命中情况，供调参。"""
    out = []
    for hit in evaluate(db, filename, text):
        r = hit.rule
        out.append({
            "rule_id": r.id, "name": r.name, "pattern": r.pattern, "match_type": r.match_type,
            "field": r.field, "document_type": r.document_type, "category_id": r.category_id,
            "confidence": hit.score, "priority": r.priority,
        })
    return out