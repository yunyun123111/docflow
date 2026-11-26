"""默认种子数据：分类树 + 关键词 + 基础规则 + 重命名模板。首次启动写入。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from backend.models import Category, CategoryKeyword, ClassifyRule, DocRequirement, RenameTemplate

DEFAULT_TREE: dict[str, tuple[list[str], list[str]]] = {
    "合同": (["采购合同", "销售合同", "框架合同", "补充协议", "结算单", "货权转移"],
             ["合同", "协议", "签署"]),
    "财务": (["发票", "银行回单", "对账单", "收付款凭证"], ["发票", "回单", "转账", "付款", "收款"]),
    "业务": (["报价单", "订单", "出货单", "入库单", "提货单"],
             ["报价", "订单", "出货", "入库", "提货"]),
    "项目资料": ([], ["方案", "验收", "投标"]),
    "证照": ([], ["执照", "许可", "资质"]),
    "其他": ([], []),
}

DEFAULT_TYPE_RULES = [
    ("发票识别", "发票", "keyword", "发票", 0.92),
    ("银行回单", "银行回单", "keyword", "回单", 0.9),
    ("结算单", "结算单", "keyword", "结算", 0.88),
    ("合同识别", "合同", "keyword", "合同", 0.9),
    ("订单识别", "订单", "keyword", "订单", 0.85),
    ("报价单", "报价单", "keyword", "报价", 0.85),
    ("出货单", "出货单", "keyword", "出货", 0.85),
    ("提货单", "提货单", "keyword", "提货", 0.86),
    ("入库单", "入库单", "keyword", "入库", 0.88),   # 采购合同必备单据，缺了规则就永远识别不出来
    ("收付款凭证", "收付款凭证", "regex",
     "收款凭证|付款凭证|转账凭证|进账单|电子回单|支付凭证", 0.88),
]

# 必备单据清单：决定“缺发票/缺提货单”这类可上传补齐的差异。
# 回款凭证不在此表（已有 missing_payment 管），避免同一件事报两条。
DEFAULT_REQUIREMENTS: dict[str, list[str]] = {
    "销售合同": ["发票", "结算单", "提货单"],
    "采购合同": ["发票", "结算单", "入库单"],
}


def seed_defaults(db: Session) -> None:
    if db.query(Category).count():
        return
    order = 0
    for parent_name, (children, keywords) in DEFAULT_TREE.items():
        parent = Category(name=parent_name, path=parent_name, sort_order=order)
        db.add(parent)
        db.flush()
        for kw in keywords:
            db.add(CategoryKeyword(category_id=parent.id, keyword=kw))
        order += 1
        for child in children:
            db.add(Category(name=child, path=f"{parent_name}/{child}", parent_id=parent.id, sort_order=order))
            order += 1
    for name, doc_type, mt, pattern, conf in DEFAULT_TYPE_RULES:
        db.add(ClassifyRule(name=name, pattern=pattern, match_type=mt, field="any",
                            document_type=doc_type, confidence=conf))
    db.add(RenameTemplate(name="通用", document_type="", pattern="{date}{underscore}{contract_no}{underscore}{title}", is_default=True))
    db.add(RenameTemplate(name="发票", document_type="发票", pattern="发票_{date}{underscore}{amount}元"))
    db.commit()


def _find_cat(db: Session, name: str, parent_id: int | None):
    q = db.query(Category).filter(Category.name == name)
    return q.filter(Category.parent_id == parent_id if parent_id is not None
                    else Category.parent_id.is_(None)).first()


def seed_missing(db: Session) -> None:
    """幂等补齐：老库也要拿到新增的分类/关键词/规则/必备清单。

    seed_defaults 只在空表时写入，所以已升级的库永远没有“提货单”这类新类型；
    这里按名称逐项补，已存在的一律跳过，不覆盖用户自己改过的东西。
    """
    added = False
    order = (db.query(Category).count() or 0) + 100
    for parent_name, (children, keywords) in DEFAULT_TREE.items():
        parent = _find_cat(db, parent_name, None)
        if parent is None:
            parent = Category(name=parent_name, path=parent_name, sort_order=order)
            db.add(parent)
            db.flush()
            order += 1
            added = True
        have_kw = {k.keyword for k in parent.keywords}
        for kw in keywords:
            if kw not in have_kw:
                db.add(CategoryKeyword(category_id=parent.id, keyword=kw))
                added = True
        for child in children:
            if _find_cat(db, child, parent.id) is None:
                db.add(Category(name=child, path=f"{parent_name}/{child}",
                                parent_id=parent.id, sort_order=order))
                order += 1
                added = True

    have_rule = {r.name for r in db.query(ClassifyRule).all()}
    for name, doc_type, mt, pattern, conf in DEFAULT_TYPE_RULES:
        if name not in have_rule:
            db.add(ClassifyRule(name=name, pattern=pattern, match_type=mt, field="any",
                                document_type=doc_type, confidence=conf))
            added = True

    have_req = {(r.contract_role, r.required_role) for r in db.query(DocRequirement).all()}
    for contract_role, req_roles in DEFAULT_REQUIREMENTS.items():
        for req_role in req_roles:
            if (contract_role, req_role) not in have_req:
                db.add(DocRequirement(contract_role=contract_role, required_role=req_role))
                added = True

    if added:
        db.commit()
