"""文档归集（业务包）。

一份业务的采购合同、销售合同，以及它们各自的发票/结算单/提货单等，按合同编号自动
归进同一个 Bundle；包内按「合同锚点」分组，每组按必备单据清单算齐备度。
对账差异直接引用这里的分组与齐备结果，两个功能互相印证。

约定：
- 一个文档只属于一个包（bundle_items 唯一约束），改合同号就是把条目搬走。
- 一个合同号只有一个包（contract_key 唯一索引）；竞态或手工认领造出重复包时 fold_same_key 并掉。
- 没有合同编号的文档不自动入包，只能在归集页手工加。
- 包名/方向/金额等汇总字段是派生值，refresh_bundle 统一重算，别在调用方手改。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import (
    Bundle, BundleItem, Category, DocRequirement, Document, DocumentField,
    STATUS_ARCHIVED, STATUS_RECYCLED,
)

CONTRACT_ROLES = ("销售合同", "采购合同", "框架合同", "补充协议")
ROLE_OTHER = "其他"

# 归档流水线是多线程的：同一个合同号的合同和发票可能在两条线程里同时入包，而"查包→建包"
# 不是原子的，两个线程都查不到就会各建一个包，把一个业务劈成两半。建包/并包一律串行。
_BUNDLE_LOCK = threading.RLock()
# 吸包过程中不要递归再吸一次（merge/refresh 会绕回来）
_FOLDING = threading.local()

# 归档分类常常只到父级「合同」，销售/采购分不开，必备单据清单就一条都匹配不上。
# 这时改用标题性证据：文件名和正文抬头写的是什么合同，它在业务里就充当什么角色。
TITLE_ROLE_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("销售合同", ("销售合同", "销货合同", "出售合同", "销购合同")),
    ("采购合同", ("采购合同", "购货合同", "订货合同", "采购协议", "购销合同")),
    ("框架合同", ("框架合同", "框架协议", "年度框架")),
    ("补充协议", ("补充协议", "补充合同", "变更协议")),
)


class BundleError(Exception):
    pass


def _helpers():
    """延迟导入，避开 reconciliation <-> bundles 的模块级循环。"""
    from backend.services.reconciliation import (
        amount_close, contract_variants, norm_no, norm_text, to_date, to_decimal,
    )

    return amount_close, contract_variants, norm_no, norm_text, to_date, to_decimal


def items_of(db: Session, bundle_id: int) -> list[BundleItem]:
    """包内条目显式查询。

    session 是 expire_on_commit=False，条目被搬到别的包之后，父包已加载的
    items 关系里还会留着它；cascade 会把这种"看起来还属于本包"的行一起删掉。
    所以凡是要判断归属的地方都重新查，不吃关系缓存。
    """
    return (db.query(BundleItem).filter(BundleItem.bundle_id == bundle_id)
            .order_by(BundleItem.id).all())


def field_map(db: Session, doc_ids: list[int]) -> dict[int, dict[str, str]]:
    """一次查询把多篇文档的抽取字段取回来，避免逐篇 N+1。"""
    out: dict[int, dict[str, str]] = {i: {} for i in doc_ids}
    if not doc_ids:
        return out
    for f in db.query(DocumentField).filter(DocumentField.document_id.in_(doc_ids)).all():
        out.setdefault(f.document_id, {})[f.name] = f.value or ""
    return out


def doc_field(db: Session, doc: Document, name: str) -> str:
    f = db.query(DocumentField).filter(DocumentField.document_id == doc.id,
                                       DocumentField.name == name).first()
    return (f.value or "") if f else ""


def contract_no_of(db: Session, doc: Document) -> str:
    from backend.services.fields import FIELD_CONTRACT_NO

    return doc_field(db, doc, FIELD_CONTRACT_NO)


def category_leaf(db: Session, doc: Document) -> str:
    cat_id = doc.archived_category_id or doc.suggested_category_id
    if not cat_id:
        return ""
    cat = db.get(Category, cat_id)
    if not cat:
        return ""
    return (cat.path or cat.name).rstrip("/").split("/")[-1]


def header_text(db: Session, doc: Document) -> str:
    """正文抬头（前几行）：合同标题通常就在这里。与规则引擎同一口径。"""
    from backend.models import DocumentText
    from backend.services.rules import header_span

    row = db.get(DocumentText, doc.id)
    if not row:
        return ""
    return header_span(row.text)[0]


def infer_contract_role(db: Session, doc: Document) -> str:
    """分类只到「合同」时按标题证据推断销售/采购等角色；推不出返回空串。"""
    names = (doc.current_filename or "", doc.original_filename or "", doc.title or "")
    for scope in (names, (header_text(db, doc),)):
        for role, words in TITLE_ROLE_WORDS:
            if any(w in src for src in scope for w in words):
                return role
    return ""


def derive_role(db: Session, doc: Document) -> str:
    """合同类取分类叶子（销售合同/采购合同…），其余取 document_type，兜底「其他」。

    真实归档常常只落在父级「合同」，而 document_type 已经细到结算单/提货单，
    所以判断顺序是：合同专用叶子 → 具体单据类型 → 合同（父级叶子或类型就叫合同）再推角色。
    """
    leaf = category_leaf(db, doc)
    if leaf in CONTRACT_ROLES:
        return leaf
    dt = (doc.document_type or "").strip()
    if dt in CONTRACT_ROLES:
        return dt
    if dt and dt != "合同":
        return dt
    if dt == "合同" or "合同" in leaf:
        # 叶子只到父级：先看标题证据，再退回分类/类型
        return infer_contract_role(db, doc) or leaf or "合同"
    return dt or leaf or ROLE_OTHER


def is_contract_role(role: str) -> bool:
    return role in CONTRACT_ROLES or role == "合同"


def _parties_of(fields: dict[str, str]) -> tuple[str, str]:
    """(我方主体, 对方客户)：本公司名称设置优先，且两者绝不返回同一个值。

    从前 fields 把文中第一个公司名同时写进「公司名称」和「对方单位」，于是包上的客户
    和我方主体都是对方，合并建议拿我方主体比对永远相等，配对全错。
    """
    from backend.config import settings
    from backend.services.fields import FIELD_COMPANY, FIELD_COUNTERPARTY

    _, _, _, norm_text, _, _ = _helpers()
    party = (fields.get(FIELD_COUNTERPARTY, "") or "").strip()
    company = (fields.get(FIELD_COMPANY, "") or "").strip()
    ours = (settings.OUR_COMPANY or "").strip()
    if not ours:
        # 没填本公司：只有两个名字确实不同才敢说第二个是我方，否则宁可留空
        ours = company if company and norm_text(company) != norm_text(party) else ""
    other = party if party and norm_text(party) != norm_text(ours) else (
        company if company and norm_text(company) != norm_text(ours) else "")
    return ours, other


def auto_name(cno: str, customer: str) -> str:
    """自动包名的唯一口径：合同号 · 客户。刷新时按同一口径重算，别留着建档时的旧客户。"""
    return " · ".join(x for x in (cno, customer) if x)


def get_or_create_bundle(db: Session, key: str, doc: Document, fields: dict[str, str]) -> Bundle:
    _, _, norm_no, _, _, _ = _helpers()
    # source 倒序：manual > auto，手工包优先当同一个合同号的家（用户自己建的包不能被程序抢走）
    bundle = (db.query(Bundle).filter(Bundle.contract_key == key)
              .order_by(Bundle.source.desc(), Bundle.id.asc()).first())
    if bundle:
        return bundle
    from backend.services.fields import FIELD_CONTRACT_NO

    cno = fields.get(FIELD_CONTRACT_NO, "")
    ours, customer = _parties_of(fields)
    name = auto_name(cno or key, customer) or (doc.current_filename or doc.original_filename)
    bundle = Bundle(name=name[:200], contract_key=norm_no(cno) or key, customer=customer[:200],
                    our_entity=ours[:200], source="auto")
    try:
        with db.begin_nested():      # 保存点：撞了唯一索引也不牵连本线程其它改动
            db.add(bundle)
            db.flush()
    except IntegrityError:
        # 另一个线程刚好把同合同号的包建出来了，用它的
        winner = db.query(Bundle).filter(Bundle.contract_key == key).first()
        if winner is not None:
            return winner
        raise
    return bundle


def attach_document(db: Session, doc: Document, added_by: str = "auto",
                    role: str | None = None) -> Bundle | None:
    """归档后入包；已属于别的包就搬过来（合同号被人工改过就走这条）。"""
    with _BUNDLE_LOCK:
        # 结束本线程可能早于对手提交的只读快照，重新读一次包表再决定建不建包
        db.commit()
        return _attach_document(db, doc, added_by, role)


def _attach_document(db: Session, doc: Document, added_by: str = "auto",
                     role: str | None = None) -> Bundle | None:
    if doc.status == STATUS_RECYCLED:
        return None
    _, _, norm_no, _, _, _ = _helpers()
    fields = field_map(db, [doc.id]).get(doc.id, {})
    from backend.services.fields import FIELD_CONTRACT_NO

    cno = fields.get(FIELD_CONTRACT_NO, "")
    if not cno.strip():
        return None
    key = norm_no(cno)
    if not key:
        return None        # 编号全是标点，归一化后是空号，别把它塞进某个手工包
    bundle = get_or_create_bundle(db, key, doc, fields)
    role = role or derive_role(db, doc)
    item = db.query(BundleItem).filter(BundleItem.document_id == doc.id).first()
    prev_id = item.bundle_id if item else None
    if item:
        item.bundle_id = bundle.id
        item.contract_no = cno
        if added_by == "manual" or item.added_by != "manual":
            item.added_by = added_by
        if role:
            item.role = role
    else:
        db.add(BundleItem(bundle_id=bundle.id, document_id=doc.id, role=role,
                         contract_no=cno, added_by=added_by))
    db.flush()
    refresh_bundle(db, bundle)
    bundle.updated_at = datetime.now()
    fold_same_key(db, bundle)
    if prev_id and prev_id != bundle.id:
        settle_bundle(db, prev_id)
    db.commit()
    return bundle


def settle_bundle(db: Session, bundle_id: int | None, prune_empty: bool = True) -> None:
    """条目离开某个包之后收尾：还有文档就重算派生字段，auto 空包就删掉。

    空的 auto 包一定要删：它还占着 contract_key，get_or_create_bundle 会先撞上它，
    让之后同合同号的归档文档被装进一个用户看不到的空壳里。
    """
    if not bundle_id:
        return
    db.expire_all()
    bundle = db.get(Bundle, bundle_id)
    if bundle is None:
        return
    if items_of(db, bundle.id):
        refresh_bundle(db, bundle)
        bundle.updated_at = datetime.now()
        return
    if prune_empty and bundle.source == "auto":
        db.delete(bundle)


def detach_document(db: Session, doc_id: int, keep_empty: bool = False) -> None:
    item = db.query(BundleItem).filter(BundleItem.document_id == doc_id).first()
    if not item:
        return
    bundle_id = item.bundle_id
    db.delete(item)
    db.flush()
    settle_bundle(db, bundle_id, prune_empty=not keep_empty)
    db.commit()


def _direction(roles: set[str]) -> str:
    has_sale, has_purchase = "销售合同" in roles, "采购合同" in roles
    if has_sale and has_purchase:
        return "mixed"
    if has_sale:
        return "sale"
    if has_purchase:
        return "purchase"
    return ""


def _move_items(db: Session, from_id: int, to_id: int) -> int:
    """把源包的条目搬进目标包，返回搬了多少条。

    脏数据里同一个文档可能在两个包各有一行（旧版并发入包留下的），直接搬会撞
    bundle_items 的唯一约束，所以先把目标包已有的那些重复行丢掉：文档留在目标包才对。
    """
    kept = db.query(BundleItem.document_id).filter(BundleItem.bundle_id == to_id)
    db.query(BundleItem).filter(BundleItem.bundle_id == from_id,
                                BundleItem.document_id.in_(kept)).delete(
        synchronize_session=False)
    return db.query(BundleItem).filter(BundleItem.bundle_id == from_id).update(
        {"bundle_id": to_id}, synchronize_session=False)


def _take_key(db: Session, bundle: Bundle, new_key: str) -> bool:
    """这个包要接手一个已经被别的包占着的合同号：先把对方吸进来再改名。

    contract_key 上有唯一索引，直接改名会在"源包还没删掉"的那一刻撞索引；
    所以这里只搬家+删包，本包的合同号留给 refresh_bundle 继续算。
    """
    if getattr(_FOLDING, "on", False) or not new_key or new_key == bundle.contract_key:
        return False
    owner = (db.query(Bundle).filter(Bundle.contract_key == new_key, Bundle.id != bundle.id)
             .order_by(Bundle.source.desc(), Bundle.id.asc()).first())
    if owner is None:
        return False
    _FOLDING.on = True
    try:
        with _BUNDLE_LOCK:
            moved = _move_items(db, owner.id, bundle.id)
            name, oid = owner.name, owner.id
            db.flush()
            db.expire_all()
            db.delete(db.get(Bundle, oid))
            from backend.services.archive import log_operation

            log_operation(db, "bundle_fold",
                          detail=f"#{oid}《{name}》并入 #{bundle.id}，合同号 {new_key}（{moved} 条）")
            db.commit()
    finally:
        _FOLDING.on = False
    return True


def refresh_bundle(db: Session, bundle: Bundle) -> Bundle:
    """重算方向/主合同号/客户/金额/签订日期（全部由包内文档派生）。"""
    from backend.services.fields import (
        FIELD_AMOUNT, FIELD_COMPANY, FIELD_CONTRACT_NO, FIELD_COUNTERPARTY, FIELD_DATE,
    )

    items = items_of(db, bundle.id)
    if not items:
        return bundle
    docs = {i.document_id: db.get(Document, i.document_id) for i in items}
    docs = {k: v for k, v in docs.items() if v is not None and v.status != STATUS_RECYCLED}
    fmap = field_map(db, list(docs))
    roles = {i.role for i in items}
    anchors = [i for i in items if is_contract_role(i.role)]
    primary = anchors[0] if anchors else items[0]
    _, _, norm_no, _, to_date, to_decimal = _helpers()

    bundle.direction = _direction(roles)
    pdoc = docs.get(primary.document_id)
    if pdoc is not None:
        pf = fmap.get(pdoc.id, {})
        new_key = norm_no(pf.get(FIELD_CONTRACT_NO, "")) or bundle.contract_key
        if new_key and new_key != bundle.contract_key and _take_key(db, bundle, new_key):
            return refresh_bundle(db, bundle)   # 条目变了，整套派生值重算一遍
        bundle.contract_key = new_key
        ours, other = _parties_of(pf)
        # 设置里的本公司名称直接生效，不必等用户重新识别一遍历史文档
        if ours:
            bundle.our_entity = ours[:200]
        if other:
            bundle.customer = other[:200]
        bundle.amount = to_decimal(pf.get(FIELD_AMOUNT, ""))
        bundle.sign_date = to_date(pf.get(FIELD_DATE, ""))
        if bundle.source == "auto":
            # 包名跟着主合同走：先建档的是发票时，客户名会晚一步才知道
            named = auto_name(pf.get(FIELD_CONTRACT_NO, "") or primary.contract_no, bundle.customer)
            if named:
                bundle.name = named[:200]
    if not bundle.name and pdoc is not None:
        bundle.name = (pdoc.current_filename or pdoc.original_filename)[:200]
    return bundle


def requirements_map(db: Session) -> dict[str, list[DocRequirement]]:
    out: dict[str, list[DocRequirement]] = {}
    for r in db.query(DocRequirement).order_by(DocRequirement.id).all():
        if r.enabled:
            out.setdefault(r.contract_role, []).append(r)
    return out


def _doc_out(doc: Document, fields: dict[str, str], item: BundleItem) -> dict:
    _, _, _, _, to_date, to_decimal = _helpers()
    from backend.services.fields import FIELD_AMOUNT, FIELD_DATE
    from backend.services.recycle import doc_file_missing

    return {
        "item_id": item.id, "document_id": doc.id, "role": item.role,
        "contract_no": item.contract_no,
        "filename": doc.current_filename or doc.original_filename,
        "file_type": doc.file_type, "document_type": doc.document_type,
        "status": doc.status, "confidence": doc.confidence, "added_by": item.added_by,
        "has_seal": doc.has_seal,
        "amount": to_decimal(fields.get(FIELD_AMOUNT, "")),
        "doc_date": to_date(fields.get(FIELD_DATE, "")),
        "file_exists": not doc_file_missing(doc),
    }


def bundle_report(db: Session, bundle: Bundle, reqs: dict[str, list[DocRequirement]] | None = None,
                  diff_counts: dict[str, int] | None = None) -> dict:
    """一个包的齐备度视图；分组键 = 合同锚点的归一化合同号。"""
    amount_close, contract_variants, norm_no, _, to_date, to_decimal = _helpers()
    from backend.services.fields import FIELD_AMOUNT, FIELD_CONTRACT_NO, FIELD_DATE
    from backend.services.reconciliation import SETTLE_DOC_TYPES

    reqs = reqs if reqs is not None else requirements_map(db)
    items = sorted(items_of(db, bundle.id), key=lambda i: (0 if is_contract_role(i.role) else 1, i.id))
    docs = {i.document_id: db.get(Document, i.document_id) for i in items}
    docs = {k: v for k, v in docs.items()
            if v is not None and v.status != STATUS_RECYCLED}
    fmap = field_map(db, list(docs))

    groups: dict[str, dict] = {}
    order: list[str] = []
    for item in items:
        doc = docs.get(item.document_id)
        if doc is None:
            continue
        f = fmap.get(doc.id, {})
        gkey = norm_no(f.get(FIELD_CONTRACT_NO, "") or item.contract_no) or "_"
        if gkey not in groups:
            groups[gkey] = {"contract_no": (f.get(FIELD_CONTRACT_NO, "") or item.contract_no),
                            "anchors": [], "attachments": []}
            order.append(gkey)
        row = _doc_out(doc, f, item)
        if is_contract_role(item.role):
            groups[gkey]["anchors"].append(row)
        else:
            groups[gkey]["attachments"].append(row)

    out_groups, need_total, have_total, missing_all = [], 0, 0, []
    for gkey in order:
        g = groups[gkey]
        anchor_roles = {a["role"] for a in g["anchors"]}
        contract_amount = next((a["amount"] for a in g["anchors"] if a["amount"] is not None), None)
        sign_date = next((a["doc_date"] for a in g["anchors"] if a["doc_date"]), "")
        required: list[dict] = []
        seen_req: set[str] = set()
        for role in anchor_roles:
            for r in reqs.get(role, []):
                if r.required_role in seen_req:
                    continue
                seen_req.add(r.required_role)
                got = [a for a in g["attachments"] if a["role"] == r.required_role]
                ok = len(got) >= r.min_count
                actual = sum(a["amount"] or 0 for a in got) if got else None
                amount_ok = None
                if r.amount_check and contract_amount is not None and actual:
                    amount_ok = abs(actual - contract_amount) <= max(1.0, 0.005 * max(abs(contract_amount), abs(actual)))
                required.append({"role": r.required_role, "present": len(got), "min_count": r.min_count,
                                 "ok": ok, "amount_check": r.amount_check, "expected": contract_amount,
                                 "actual": actual, "amount_ok": amount_ok})
                need_total += 1
                have_total += 1 if ok else 0
                if not ok:
                    missing_all.append(r.required_role)
        anchor_amount = next((a["amount"] for a in g["anchors"] if a["amount"] is not None), None)
        # 「已结算」按单据类型分桶，与对账的 worst_amount_bucket 同一口径：发票合计与结算单合计
        # 说的是同一笔钱，把它们相加会凭空造出一个红色差额（齐备的包也被标成金额不符）。
        buckets: dict[str, float] = {}
        for a in g["attachments"]:
            if a["role"] in SETTLE_DOC_TYPES and a["amount"]:
                buckets[a["role"]] = buckets.get(a["role"], 0.0) + a["amount"]
        settle_sum, settle_role, amount_gap = 0.0, "", None
        if buckets:
            if anchor_amount is None:
                settle_role, settle_sum = max(buckets.items(), key=lambda kv: kv[1])
            else:
                bad = {r: v for r, v in buckets.items() if not amount_close(v, anchor_amount)}
                if bad:      # 有一类单据对不上就拿那一类当依据，差异才指得准
                    settle_role, settle_sum = max(
                        bad.items(), key=lambda kv: abs(kv[1] - anchor_amount))
                    amount_gap = round(settle_sum - anchor_amount, 2)
                else:        # 全都对得上就显示最贴近合同金额的那一类，不标差额
                    settle_role, settle_sum = min(
                        buckets.items(), key=lambda kv: abs(kv[1] - anchor_amount))
        groups[gkey]["required"] = required
        groups[gkey]["missing"] = [q["role"] for q in required if not q["ok"]]
        groups[gkey]["amount"] = anchor_amount
        groups[gkey]["sign_date"] = sign_date
        groups[gkey]["settlement_sum"] = settle_sum or None
        groups[gkey]["settlement_role"] = settle_role
        groups[gkey]["amount_gap"] = amount_gap
        out_groups.append({"contract_no": g["contract_no"], "anchor_roles": sorted(anchor_roles),
                           "anchors": g["anchors"], "attachments": g["attachments"],
                           "required": required, "missing": groups[gkey]["missing"],
                           "amount": anchor_amount, "sign_date": sign_date,
                           "settlement_sum": groups[gkey]["settlement_sum"],
                           "settlement_role": groups[gkey].get("settlement_role", ""),
                           "amount_gap": groups[gkey]["amount_gap"]})

    contract_nos = [g["contract_no"] for g in out_groups if g["contract_no"]]
    variants: set[str] = set()
    for c in contract_nos:
        variants |= contract_variants(c)
    diff_count = sum(v for k, v in (diff_counts or {}).items() if k in variants)

    return {
        "id": bundle.id, "name": bundle.name, "contract_key": bundle.contract_key,
        "contract_nos": contract_nos, "customer": bundle.customer,
        "our_entity": bundle.our_entity, "direction": bundle.direction,
        "amount": bundle.amount, "sign_date": bundle.sign_date, "status": bundle.status,
        "note": bundle.note, "source": bundle.source,
        "updated_at": bundle.updated_at.isoformat(timespec="seconds") if bundle.updated_at else None,
        "groups": out_groups, "item_count": len(items),
        "completeness": {"have": have_total, "need": need_total, "missing": missing_all},
        "complete": need_total > 0 and have_total == need_total,
        "diff_count": diff_count,
    }


def _latest_open_diff_counts(db: Session) -> dict[str, int]:
    """最近一期未完成处理的差异，按归一化合同号计数（供包列表显示“关联差异数”）。"""
    from backend.models import ReconDiff, ReconRun

    run = (db.query(ReconRun).filter(ReconRun.status == "completed")
           .order_by(ReconRun.id.desc()).first())
    if not run:
        return {}
    _, _, norm_no, _, _, _ = _helpers()
    out: dict[str, int] = {}
    for d in db.query(ReconDiff).filter(ReconDiff.run_id == run.id,
                                        ReconDiff.resolved.is_(False)).all():
        k = norm_no(d.contract_no)
        if k:
            out[k] = out.get(k, 0) + 1
    return out


def list_bundles(db: Session, q: str = "", state: str = "", limit: int = 200,
                offset: int = 0) -> dict:
    reqs = requirements_map(db)
    diff_counts = _latest_open_diff_counts(db)
    query = db.query(Bundle).order_by(Bundle.updated_at.desc(), Bundle.id.desc())
    total_all = query.count()
    rows = query.offset(offset).limit(min(limit, 500)).all()
    items = []
    for b in rows:
        rep = bundle_report(db, b, reqs, diff_counts)
        hay = " ".join([rep["name"], rep["customer"], rep["our_entity"],
                        " ".join(rep["contract_nos"])]).lower()
        if q and q.lower() not in hay:
            continue
        if state == "missing" and rep["complete"]:
            continue
        if state == "complete" and not rep["complete"]:
            continue
        items.append({k: v for k, v in rep.items() if k != "groups"})
    return {"total": total_all, "items": items}


def get_bundle_detail(db: Session, bundle_id: int) -> dict:
    bundle = db.get(Bundle, bundle_id)
    if not bundle:
        raise BundleError("业务包不存在")
    return bundle_report(db, bundle, diff_counts=_latest_open_diff_counts(db))


def create_bundle(db: Session, name: str = "", note: str = "") -> Bundle:
    bundle = Bundle(name=(name or "未命名业务")[:200], note=note or "", source="manual",
                    contract_key="")
    db.add(bundle)
    db.commit()
    return bundle


def add_documents(db: Session, bundle: Bundle, doc_ids: list[int],
                  role: str | None = None) -> dict:
    with _BUNDLE_LOCK:
        return _add_documents(db, bundle, doc_ids, role)


def _add_documents(db: Session, bundle: Bundle, doc_ids: list[int],
                   role: str | None = None) -> dict:
    added, skipped = [], []
    emptied: list[int] = []
    for did in doc_ids:
        doc = db.get(Document, did)
        if not doc or doc.status == STATUS_RECYCLED:
            skipped.append(did)
            continue
        item = db.query(BundleItem).filter(BundleItem.document_id == did).first()
        r = role or derive_role(db, doc)
        from backend.services.fields import FIELD_CONTRACT_NO

        cno = doc_field(db, doc, FIELD_CONTRACT_NO)
        if item:
            if item.bundle_id != bundle.id:
                emptied.append(item.bundle_id)
            item.bundle_id = bundle.id
            item.role = r
            item.contract_no = cno
            item.added_by = "manual"
        else:
            db.add(BundleItem(bundle_id=bundle.id, document_id=did, role=r, contract_no=cno,
                              added_by="manual"))
        added.append(did)
    db.flush()
    refresh_bundle(db, bundle)
    bundle.updated_at = datetime.now()
    db.commit()
    for old_id in dict.fromkeys(emptied):
        if old_id != bundle.id:
            settle_bundle(db, old_id)
    fold_same_key(db, bundle)   # 手工包认领了这个合同号，重复的自动包并进来
    db.commit()
    return {"added": added, "skipped": skipped}


def remove_document(db: Session, bundle: Bundle, document_id: int) -> None:
    item = db.query(BundleItem).filter(BundleItem.bundle_id == bundle.id,
                                       BundleItem.document_id == document_id).first()
    if not item:
        raise BundleError("该文档不在这个业务包里")
    bundle_id = bundle.id
    db.delete(item)
    db.flush()
    refresh_bundle(db, bundle)
    bundle.updated_at = datetime.now()
    db.commit()
    settle_bundle(db, bundle_id)   # 手工包不因清空而消失，auto 空包才删
    db.commit()


def set_item_role(db: Session, item_id: int, role: str) -> dict:
    item = db.get(BundleItem, item_id)
    if not item:
        raise BundleError("条目不存在")
    item.role = (role or ROLE_OTHER)[:40]
    bundle = db.get(Bundle, item.bundle_id)
    if bundle:
        refresh_bundle(db, bundle)
        bundle.updated_at = datetime.now()
    db.commit()
    return {"id": item.id, "role": item.role}


def merge_bundles(db: Session, source_id: int, target_id: int, note: str = "") -> dict:
    if source_id == target_id:
        raise BundleError("不能合并到自己")
    source, target = db.get(Bundle, source_id), db.get(Bundle, target_id)
    if not source or not target:
        raise BundleError("业务包不存在")
    moved = _move_items(db, source_id, target.id)
    source.contract_key = ""   # 先腾号：目标包接手同一个合同号时不会撞上还没删的源包
    db.flush()
    db.expire_all()
    source = db.get(Bundle, source_id)
    target = db.get(Bundle, target_id)
    refresh_bundle(db, target)
    target.updated_at = datetime.now()
    if note:
        target.note = (target.note + " " + note).strip()[:2000]
    db.delete(source)
    from backend.services.archive import log_operation

    log_operation(db, "bundle_merge", detail=f"将业务包 #{source_id} 并入 #{target_id}（{moved} 条）")
    db.commit()
    return {"target_id": target.id, "moved": moved}


def fold_same_key(db: Session, bundle: Bundle) -> int:
    """把同一合同号的重复包并进这一个（一个合同号只该有一个业务包）。

    分裂包最坑的地方在于它不报错：齐备度和关联差异被对半砍，看起来就是
    "单据明明在库里，包却说没收到"。所以每次入包/手工加单据后都顺手并一次。
    """
    if not bundle.contract_key:
        return 0
    folded = 0
    for _ in range(200):
        other = (db.query(Bundle).filter(Bundle.contract_key == bundle.contract_key,
                                          Bundle.id != bundle.id)
                 .order_by(Bundle.id.asc()).first())
        if other is None:
            break
        merge_bundles(db, other.id, bundle.id, note="自动并包：同一合同号的重复业务包")
        folded += 1
    return folded


def duplicate_key_groups(db: Session) -> list[list[int]]:
    """现在库里同合同号多出来的包组，供自检展示。"""
    grouped: dict[str, list[int]] = {}
    for b in db.query(Bundle).filter(Bundle.contract_key != "").order_by(Bundle.id).all():
        grouped.setdefault(b.contract_key, []).append(b.id)
    return [ids for ids in grouped.values() if len(ids) > 1]


def repair_duplicate_bundles(db: Session) -> int:
    """启动自愈：把历史竞态建出的重复包按合同号各并成一个（手工包优先当目标）。"""
    folded = 0
    with _BUNDLE_LOCK:
        keys = [k for (k,) in db.query(Bundle.contract_key).filter(
            Bundle.contract_key != "").distinct().all()]
        for key in keys:
            for _ in range(200):
                rows = (db.query(Bundle).filter(Bundle.contract_key == key)
                        .order_by(Bundle.source.desc(), Bundle.id.asc()).all())
                if len(rows) < 2:
                    break
                merge_bundles(db, rows[1].id, rows[0].id, note="启动自检：合并重复业务包")
                folded += 1
    return folded


def _date_gap(a: str, b: str) -> int | None:
    try:
        d1 = datetime.strptime(a, "%Y-%m-%d")
        d2 = datetime.strptime(b, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    return abs((d1 - d2).days)


def suggestions(db: Session) -> list[dict]:
    """疑似同一业务的包对：方向相反 + 我方主体相同 + 金额差在容差内 + 日期在窗口内。"""
    if not settings.BUNDLE_MERGE_SUGGEST_ENABLED:
        return []
    _, _, norm_text, _, _, _ = _helpers()
    tol = settings.BUNDLE_MERGE_AMOUNT_TOLERANCE
    window = timedelta(days=settings.BUNDLE_MERGE_DATE_WINDOW_DAYS)
    bundles = [b for b in db.query(Bundle).all() if b.direction in ("sale", "purchase")]
    out = []
    for i, a in enumerate(bundles):
        for b in bundles[i + 1:]:
            if a.direction == b.direction:
                continue
            if not a.our_entity or not b.our_entity:
                continue
            if norm_text(a.our_entity) != norm_text(b.our_entity):
                continue
            if not a.amount or not b.amount or tol < 0:
                continue
            rel = abs(a.amount - b.amount) / max(abs(a.amount), abs(b.amount))
            if rel > tol:
                continue
            gap = _date_gap(a.sign_date, b.sign_date)
            if gap is None or gap > window.days:
                continue
            sale, purchase = (a, b) if a.direction == "sale" else (b, a)
            out.append({
                "sale_bundle_id": sale.id, "purchase_bundle_id": purchase.id,
                "sale_name": sale.name, "purchase_name": purchase.name,
                "our_entity": sale.our_entity, "amount_gap": round(abs(sale.amount - purchase.amount), 2),
                "amount_gap_pct": round(rel * 100, 2),
                "date_gap_days": gap,
                "reason": "我方主体相同、金额差 "
                          f"{round(rel * 100, 2)}%、签订日期相距 {gap} 天",
            })
    return out[:50]


def rebuild_all(db: Session) -> dict:
    """全量重建自动包（历史数据回填）；手工包保留。"""
    with _BUNDLE_LOCK:
        return _rebuild_all(db)


def _rebuild_all(db: Session) -> dict:
    docs = db.query(Document).filter(Document.status == STATUS_ARCHIVED).all()
    fmap = field_map(db, [d.id for d in docs])
    attached = 0
    no_number = 0
    for b in db.query(Bundle).filter(Bundle.source == "auto").all():
        db.delete(b)
    db.commit()
    for doc in docs:
        from backend.services.fields import FIELD_CONTRACT_NO

        if not fmap.get(doc.id, {}).get(FIELD_CONTRACT_NO, "").strip():
            no_number += 1
            continue
        if db.query(BundleItem).filter(BundleItem.document_id == doc.id).first():
            continue        # 已在手工包里，别抢走
        if attach_document(db, doc):
            attached += 1
    bundles = db.query(Bundle).count()
    return {"documents": len(docs), "attached": attached, "without_contract_no": no_number,
            "bundles": bundles}
