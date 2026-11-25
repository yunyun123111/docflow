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

# 规则置信度说的是「这个词有多能定性」，但一份矿石销售合同里也会写「开具发票」「乙方提货」，
# 只按置信度排序的话 0.92 的发票规则永远压过 0.90 的合同规则——每份合同都会被归成发票。
# 所以命中位置要算进来：文件名与正文抬头命中＝这份文件是什么（满分）；正文深处命中
# ＝只是提到了别的单据（按 BODY_WEIGHT 折算），仍然能定性，只是不再压过标题。
BODY_WEIGHT = 0.55
TITLE_LINES = 4
TITLE_CHARS = 160


@dataclass
class RuleHit:
    rule: ClassifyRule
    score: float
    where: str = "body"  # title=标题性证据命中，body=正文提及命中


def header_span(text: str, limit: int = TITLE_LINES, cap: int = TITLE_CHARS) -> tuple[str, int]:
    """正文抬头（前 limit 个非空行，最多 cap 字）＋抬头在原文中的结束下标。

    结束下标用来判断正则命中的位置，抬文字符串用来做关键词的标题性证据；
    归集页推断合同角色用的也是同一个口径。
    """
    if not text:
        return "", 0
    parts: list[str] = []
    used = 0
    taken = 0
    end = 0
    pos = 0
    for line in text.splitlines():
        line_end = pos + len(line)
        pos = line_end + 1
        stripped = line.strip()
        if not stripped:
            continue
        take = min(len(stripped), cap - used)
        if take <= 0:
            break
        parts.append(stripped[:take])
        used += take
        taken += 1
        end = line_end
        if taken >= limit or used >= cap:
            break
    return "\n".join(parts), end


def evidence_text(text: str) -> str:
    """关键词规则看到的正文：合同引用字样已剥离，标题性证据保留。"""
    return _CONTRACT_REF_RE.sub("", text or "")


def _hit_at(match_type: str, pattern: str, text: str) -> int | None:
    """pattern 在 text 中首次命中的位置；未命中返回 None。"""
    if not text or not pattern:
        return None
    if match_type == "regex":
        try:
            m = re.search(pattern, text, re.I)
        except re.error:
            return None
        return m.start() if m else None
    idx = text.lower().find(pattern.lower())
    return idx if idx >= 0 else None


def _where(match_type: str, pattern: str, field: str, filename: str,
           raw: str, head: str, body: str, head_end: int) -> str:
    """规则命中的最强位置：title（文件名/抬头）优先于 body。"""
    if field in ("filename", "any") and _hit_at(match_type, pattern, filename) is not None:
        return "title"
    if field not in ("text", "any"):
        return ""
    # 关键词规则只看剥离过合同引用字样的正文；正则规则仍看原文，不改变用户自定义规则的行为
    title_src = raw if match_type == "regex" else head
    if _hit_at(match_type, pattern, title_src) is not None:
        return "title"
    if match_type == "regex":
        at = _hit_at(match_type, pattern, raw)
        return "body" if at is not None else ""
    if _hit_at(match_type, pattern, body) is not None:
        return "body"
    return ""


def evaluate(db: Session, filename: str, text: str) -> list[RuleHit]:
    """按优先级返回命中规则（同分类只保留最高分一次，分数已含命中位置权重）。"""
    raw = text or ""
    body = evidence_text(raw)
    head_raw, head_end = header_span(raw)
    head = evidence_text(head_raw)
    hits: list[RuleHit] = []
    seen: set[tuple] = set()
    rules = db.query(ClassifyRule).filter(ClassifyRule.enabled.is_(True)).order_by(ClassifyRule.priority).all()
    for rule in rules:
        where = _where(rule.match_type, rule.pattern, rule.field, filename, raw, head, body, head_end)
        if not where:
            continue
        key = (rule.category_id, rule.document_type)
        if key in seen:
            continue
        seen.add(key)
        base = float(rule.confidence or 0.0)
        score = round(base if where == "title" else base * BODY_WEIGHT, 4)
        hits.append(RuleHit(rule=rule, score=score, where=where))
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
            "confidence": hit.score, "base_confidence": float(r.confidence or 0.0),
            "where": hit.where, "priority": r.priority,
        })
    return out
