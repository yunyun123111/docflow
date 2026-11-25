"""分类聚合：规则命中 + 分类关键词打分 + 样例指纹匹配 + AI 兜底 → 最终建议。

理念沿用旧版：规则优先、AI 只在不确定时介入；样例匹配是本地零 token 通道。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from backend.models import Category, CategoryKeyword, DocumentSample
from backend.services import ai as ai_service
from backend.services.confidence import combine, field_coverage
from backend.services.fields import FIELD_CONTRACT_NO
from backend.services.fingerprint import fingerprint as make_fp
from backend.services.rules import RuleHit, evaluate, header_span
from backend.services.text_norm import normalize_for_match

DOC_STOP = {"其他"}


@dataclass
class Classification:
    document_type: str = ""
    category_id: int | None = None
    category_path: str = ""
    confidence: float = 0.0
    detail: dict = field(default_factory=dict)
    rule_hits: list[dict] = field(default_factory=list)
    ai_used: bool = False
    sample_used: bool = False


def _keyword_score(db: Session, text: str, filename: str) -> tuple[float, int | None]:
    """分类关键词打分：命中越集中、次数越多分越高。"""
    body = normalize_for_match((filename + " " + (text or ""))[:4000])
    stat: dict[int, int] = {}
    for kw, cat_id in db.query(CategoryKeyword.keyword, CategoryKeyword.category_id).all():
        c = body.count(normalize_for_match(kw))
        if c:
            stat[cat_id] = stat.get(cat_id, 0) + min(c, 5)
    if not stat:
        return 0.0, None
    cat_id = max(stat, key=stat.get)
    return min(1.0, stat[cat_id] / 6.0), cat_id


def _category_by_name(db: Session, name: str) -> int | None:
    """类型名 → 同名分类叶子（发票→财务/发票，提货单→业务/提货单）。

    种子规则的 category_id 全是空的，于是分类只能靠关键词计数——合同正文里
    「付款」「发票」出现得比「合同」还密，合同就被归到了「财务」。类型既然已经
    判定为发票/提货单/入库单，归档目录就该落回它自己的叶子分类。
    """
    if not name:
        return None
    rows = db.query(Category).filter(Category.name == name).all()
    if not rows:
        return None
    return max(rows, key=lambda c: (c.path or c.name or "").count("/")).id


def _category_by_title(db: Session, filename: str, head: str) -> int | None:
    """类型只到「合同」时按抬头细分销售/采购/框架/补充协议，与归集角色同一口径。

    分不到叶子，业务包就没有合同锚，缺发票/缺提货单这类必备单据检查一条都对不上。
    """
    from backend.services.bundles import TITLE_ROLE_WORDS

    probe = f"{filename}\n{head}"
    for role, words in TITLE_ROLE_WORDS:
        if any(w in probe for w in words):
            return _category_by_name(db, role)
    return None


def _sample_match(db: Session, text: str):
    """样例库指纹命中（同模板文档，零 token）。"""
    fp = make_fp(text)
    if not fp:
        return None
    for s in db.query(DocumentSample).filter(DocumentSample.text_norm == fp).all():
        return 0.95, s.document_type, s.category_id
    return None


def classify(db: Session, filename: str, text: str, extracted: dict) -> Classification:
    hits = evaluate(db, filename, text)
    rule_hit: RuleHit | None = hits[0] if hits else None
    rule_score = rule_hit.score if rule_hit else 0.0

    kw_score, kw_cat = _keyword_score(db, text, filename)
    f_score = field_coverage(extracted)

    sample = _sample_match(db, text)
    sample_score, sample_type, sample_cat = sample if sample else (0.0, "", None)

    rule_type = rule_hit.rule.document_type if rule_hit else ""
    cat_from_rule = rule_hit.rule.category_id if rule_hit else None
    # 样例是用户逐份确认过的同模板文档，命中即最强证据。从前它永远排在规则后面，
    # 于是「把被判成发票的结算单改回来」这种纠正只学进库、从不生效，下一份照样错。
    sample_wins = bool(sample_type) and sample_score > rule_score
    doc_type = sample_type if sample_wins else (rule_type or sample_type)
    head, _head_end = header_span(text)
    if sample_wins:
        category_id = sample_cat or cat_from_rule or _category_by_name(db, doc_type)
    else:
        category_id = (cat_from_rule or sample_cat or _category_by_title(db, filename, head)
                       or _category_by_name(db, doc_type) or kw_cat)

    ai_score, ai_type, ai_cat, ai_used = 0.0, "", None, False
    uncertain = max(rule_score, sample_score) < 0.75
    if uncertain and ai_service.ai_enabled():
        data = ai_service.classify(text, filename)
        if data:
            ai_used = True
            ai_score = data["confidence"]
            if data.get("document_type") and data["document_type"] not in DOC_STOP:
                ai_type = data["document_type"]
            cat_name = data.get("category", "")
            if cat_name and cat_name not in DOC_STOP:
                c = (db.query(Category).filter(Category.name == cat_name).first()
                     or db.query(Category).filter(Category.path.like(f"%{cat_name}%")).first())
                ai_cat = c.id if c else None
            extracted = {**extracted, **{str(k): (str(v), 0.7) for k, v in (data.get("fields") or {}).items() if v}}

    confidence, detail = combine(max(rule_score, sample_score), f_score, kw_score, ai_score)
    if not ai_used and ai_score == 0.0 and confidence < 1.0:
        # AI 通道缺席：按剩余权重归一化，否则纯规则场景永远到不了自动归档阈值
        from backend.config import settings as _s

        w = _s.confidence_weights()
        denom = 1.0 - w["ai"]
        if denom > 0:
            confidence = round(min(1.0, confidence / denom), 4)
    if rule_score >= 0.8 and f_score >= 0.5:
        # 规则与字段提取双通道一致 → 视为高确定
        confidence = max(confidence, 0.86)
    if rule_score >= 0.85 and (extracted.get(FIELD_CONTRACT_NO) or ("", 0))[1] >= 0.6:
        # 单据自带合同编号：它是某笔已有业务的附件证据，规则命中就该归档进包，
        # 否则提货单/入库单这类没有金额的单据全堆在待审核，齐备度永远补不齐。
        confidence = max(confidence, 0.86)

    final_type = doc_type or ai_type or "其他"
    final_cat = category_id or ai_cat or _category_by_name(db, final_type)
    if sample_wins:
        confidence = max(confidence, sample_score)
        detail["sample"] = sample_score

    path_name = ""
    if final_cat:
        cat = db.get(Category, final_cat)
        path_name = cat.path if cat else ""
    return Classification(
        document_type=final_type, category_id=final_cat, category_path=path_name,
        confidence=round(confidence, 4), detail=detail,
        rule_hits=[{"rule_id": h.rule.id, "name": h.rule.name, "score": h.score,
                     "where": h.where} for h in hits],
        ai_used=ai_used, sample_used=sample_wins,
    )