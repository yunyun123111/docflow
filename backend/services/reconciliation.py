"""台账对账（重构版核心）。

概念对齐旧版但收敛为可维护的三段：
1. LedgerReader：Excel 台账 → 合同行（含结算/回款汇总）。表头别名自动探测，可用 sheet_map 覆盖。
2. DocIndex：归档文档 → 合同编号变体 / (对方单位, 金额) 双通道索引。
3. Matcher：逐合同核对下游证据（结算/发票、回款/银行回单），产出差异（缺件/金额不符/多单/日期可疑/缺章）。

两种数据源：source=ledger 以台账为准，source=archive 无台账时以已归档合同为锚自对账，
检查项完全一致。每条差异都能反查依据（diff_evidence：所属业务包 + 已匹配文档 + 仍缺项），
补齐单据后 recheck_diff 就地重算并自动关闭。

差异 key 稳定（跨期对比、人工标记可结转）。报告可导出 CSV/XLSX。
"""
from __future__ import annotations

import csv
import io
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import (
    Bundle, BundleItem, Document, Ledger, LedgerRow, ReconDiff, ReconRun, STATUS_ARCHIVED,
)
from backend.services import bundles as bundle_service
from backend.services.fields import (
    FIELD_AMOUNT, FIELD_CONTRACT_NO, FIELD_COUNTERPARTY, FIELD_DATE,
)
from backend.utils.logger import get_logger

logger = get_logger("reconciliation")

AMOUNT_TOL_ABS = 1.0
AMOUNT_TOL_REL = 0.005

HEADER_ALIASES = {
    "contract_no": ["合同编号", "合同号", "框架合同号", "协议编号", "编号", "合同名称编号"],
    "customer": ["客户名称", "客户", "对方单位", "买方", "需方", "购买方", "委托方"],
    "our_entity": ["我方", "我方主体", "供方", "卖方", "销售方", "主体"],
    "amount": ["合同金额", "签约金额", "合同总价", "总价", "价款", "金额",
               # 回款/结算表常用的列名：从前只有两字的「金额」能命中，付款金额/到账日期一律读空
               "付款金额", "收款金额", "回款金额", "到账金额", "实收金额", "已收金额", "已付金额",
               "结算金额", "发票金额"],
    "sign_date": ["签订日期", "签约日期", "签署日期", "合同日期", "日期",
                  "付款日期", "到账日期", "回款日期", "收款日期", "结算日期"],
    "direction": ["业务类别", "方向", "购销类型", "类型"],
    "settlement_total": ["结算金额", "已结算金额", "结算合计"],
    "received_total": ["回款金额", "已回款金额", "累计回款", "收款金额",
                       "付款金额", "已付款金额", "累计付款", "到账金额", "实收金额", "已收金额"],
    "contract_name": ["合同名称", "项目名称", "标的"],
}

SHEET_KIND_HINTS = {
    "contract": ["合同", "业务", "台账"],
    "settlement": ["结算", "发票"],
    "payment": ["回款", "收款", "付款", "到账"],
}

SETTLE_DOC_TYPES = {"发票", "结算单", "出货单"}
PAY_DOC_TYPES = {"银行回单", "收付款凭证"}


def norm_text(v) -> str:
    s = str(v if v is not None else "")
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s)).lower()


def norm_no(v) -> str:
    s = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]", "", str(v or ""))
    return s.upper()


def contract_variants(v) -> set[str]:
    base = norm_no(v)
    out = set()
    if base:
        out.add(base)
        m = re.search(r"(\d{6,})", base)
        if m:
            out.add(m.group(1))
    return out


def to_decimal(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return round(float(str(v).replace(",", "").replace("￥", "").replace("¥", "")), 2)
    except ValueError:
        return None


def to_date(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d") if isinstance(v, datetime) else v.isoformat()
    s = str(v).strip()
    m = re.search(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def amount_close(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= max(AMOUNT_TOL_ABS, AMOUNT_TOL_REL * max(abs(a), abs(b)))


def pay_close(a: float | None, b: float | None) -> bool:
    """回款/付款专用容差（默认 1%）：银行手续费、尾差、分批到账是常态，
    拿结算那套 0.5% 去比回款会天天刷屏；绝对下限仍沿用 1 元。"""
    from backend.config import settings

    if a is None or b is None:
        return False
    rel = settings.RECON_PAYMENT_TOLERANCE_PCT or 0.0
    return abs(a - b) <= max(AMOUNT_TOL_ABS, rel * max(abs(a), abs(b)))


# 凭证名 / 该收付 / 已收付 / 未收付 / 钱名：方向只改文案，不改类型名与判定口径
PAY_WORDS: dict[str, tuple[str, str, str, str, str]] = {
    "sale": ("回款凭证", "应收", "已收", "未收", "回款"),
    "purchase": ("付款凭证", "应付", "已付", "未付", "付款"),
    "mixed": ("收付凭证", "该收付", "已收付", "未收付", "收付"),
}


def _pay_words(direction: str) -> tuple[str, str, str, str, str]:
    return PAY_WORDS.get((direction or "").strip().lower(), PAY_WORDS["mixed"])


def _norm_direction(v) -> str:
    """台账里的业务类别是中文（销售/采购），包上的方向是英文，统一成一个口径。"""
    s = norm_text(v)
    if not s:
        return ""
    if s in ("sale", "sell", "销售"):
        return "sale"
    if s in ("purchase", "buy", "采购", "购买"):
        return "purchase"
    if ("销" in s and ("采" in s or "购" in s)) or s in ("mixed", "购销", "既销既购"):
        return "mixed"
    if "销" in s:
        return "sale"
    if "采" in s or "购" in s:
        return "purchase"
    return ""


# ---------------- LedgerReader ----------------

def _find_col(headers: list[str], key: str) -> int | None:
    normed = [norm_text(h) for h in headers]
    for alias in HEADER_ALIASES[key]:
        na = norm_text(alias)
        for i, h in enumerate(normed):
            if h == na or (len(na) >= 3 and na in h):
                return i
    return None


def _detect_sheet_kind(headers: list[str]) -> str | None:
    text = "|".join(norm_text(h) for h in headers)
    has_no = _find_col(headers, "contract_no") is not None
    has_amount = _find_col(headers, "amount") is not None
    has_received = _find_col(headers, "received_total") is not None
    if has_no and has_amount and not has_received:
        if any(k in text for k in ("回款", "收款", "到账")):
            return "payment"
        if any(k in text for k in ("结算",)):
            return "settlement"
        return "contract"
    if any(k in text for k in ("回款", "收款", "到账")):
        return "payment"
    if any(k in text for k in ("结算", "发票")):
        return "settlement"
    return None


@dataclass
class ParsedSheet:
    kind: str
    headers: list[str]
    rows: list[list]


def read_ledger_file(path: str | Path, sheet_map: dict | None = None) -> dict:
    """返回 {sheets: {name:kind}, contract:settlement:payment: rows}。"""
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    sheets: dict[str, str] = {}
    collected: dict[str, list[dict]] = {"contract": [], "settlement": [], "payment": []}
    for ws in wb.worksheets:
        rows_iter = ws.iter_rows(values_only=True)
        try:
            headers = list(next(rows_iter))
        except StopIteration:
            continue
        kind = (sheet_map or {}).get(ws.title) or _detect_sheet_kind([str(h or "") for h in headers])
        sheets[ws.title] = kind or "unknown"
        if not kind:
            continue
        cols = {k: _find_col([str(h or "") for h in headers], k) for k in HEADER_ALIASES}
        for raw in rows_iter:
            if raw is None:
                continue

            def cell(key):
                i = cols.get(key)
                if i is None or i >= len(raw):
                    return None
                return raw[i]

            if kind == "contract":
                cno = cell("contract_no")
                if not cno or not norm_no(cno):
                    continue
                collected["contract"].append({
                    "contract_no": str(cno).strip(),
                    "customer": str(cell("customer") or "").strip(),
                    "our_entity": str(cell("our_entity") or "").strip(),
                    "amount": to_decimal(cell("amount")),
                    "sign_date": to_date(cell("sign_date")),
                    "direction": str(cell("direction") or "").strip(),
                    "settlement_total": to_decimal(cell("settlement_total")),
                    "received_total": to_decimal(cell("received_total")),
                    "name": str(cell("contract_name") or "").strip(),
                })
            else:
                entry = {
                    "contract_no": str(cell("contract_no") or "").strip(),
                    "customer": str(cell("customer") or "").strip(),
                    "amount": to_decimal(cell("amount")),
                    "date": to_date(cell("sign_date")),
                    "sheet": ws.title,
                }
                if entry["contract_no"] or entry["amount"]:
                    collected[kind].append(entry)
    wb.close()
    return {"sheets": sheets, **collected}


def save_ledger(db: Session, name: str, path: str, parsed: dict, sheet_map: dict | None = None) -> Ledger:
    ledger = Ledger(name=name, path=str(path), sheet_map=sheet_map or parsed["sheets"])
    db.add(ledger)
    db.flush()
    settle_by_no: dict[str, float] = {}
    pay_by_no: dict[str, float] = {}
    for r in parsed["settlement"]:
        for v in contract_variants(r["contract_no"]):
            settle_by_no[v] = settle_by_no.get(v, 0) + (r["amount"] or 0)
    for r in parsed["payment"]:
        for v in contract_variants(r["contract_no"]):
            pay_by_no[v] = pay_by_no.get(v, 0) + (r["amount"] or 0)
    for r in parsed["contract"]:
        variants = contract_variants(r["contract_no"])
        st = next((settle_by_no[v] for v in variants if v in settle_by_no), r["settlement_total"])
        pr = next((pay_by_no[v] for v in variants if v in pay_by_no), r["received_total"])
        db.add(LedgerRow(
            ledger_id=ledger.id, contract_no=r["contract_no"], customer=r["customer"],
            our_entity=r["our_entity"], direction=r["direction"], amount=r["amount"],
            sign_date=r["sign_date"], settlement_total=st, received_total=pr,
            extra={"name": r["name"], "sheet": "contract"},
        ))
    db.commit()
    return ledger


# ---------------- DocIndex ----------------

@dataclass
class DocRecord:
    id: int
    filename: str
    doc_type: str
    contract_no: str
    customer: str
    amount: float | None
    doc_date: str
    has_seal: bool | None
    variants: set[str] = field(default_factory=set)
    role: str = ""


@dataclass
class Matches:
    """一份台账行（或一份合同）在档案库里找到的证据，按角色分好。"""
    all: list[DocRecord] = field(default_factory=list)
    settle: list[DocRecord] = field(default_factory=list)
    pay: list[DocRecord] = field(default_factory=list)
    anchors: list[DocRecord] = field(default_factory=list)

    def of_role(self, role: str) -> list[DocRecord]:
        return [d for d in self.all if d.role == role]

    @property
    def doc_ids(self) -> set[int]:
        return {d.id for d in self.all}


def _doc_field(doc: Document, name: str) -> str:
    for f in doc.fields:
        if f.name == name:
            return f.value or ""
    return ""


def build_doc_index(db: Session) -> list[DocRecord]:
    docs = db.query(Document).filter(Document.status == STATUS_ARCHIVED).all()
    out: list[DocRecord] = []
    for d in docs:
        cno = _doc_field(d, FIELD_CONTRACT_NO)
        rec = DocRecord(
            id=d.id, filename=d.current_filename or d.original_filename,
            doc_type=d.document_type or "", contract_no=cno,
            customer=_doc_field(d, FIELD_COUNTERPARTY),
            amount=to_decimal(_doc_field(d, FIELD_AMOUNT)),
            doc_date=_doc_field(d, FIELD_DATE), has_seal=d.has_seal,
            variants=contract_variants(cno), role=bundle_service.derive_role(db, d),
        )
        out.append(rec)
    return out


def _match_by_name(docs: list[DocRecord], customer: str, amount: float | None) -> list[DocRecord]:
    """编号通道落空时的兜底：对方单位互含 + 金额不冲突。"""
    cust = norm_text(customer)
    out = []
    for d in docs:
        dcust = norm_text(d.customer)
        if not (cust and dcust and (cust in dcust or dcust in cust)):
            continue
        if amount is None or d.amount is None or amount_close(d.amount, amount):
            out.append(d)
    return out


def _split(docs: list[DocRecord]) -> Matches:
    return Matches(
        all=list(docs),
        settle=[d for d in docs if d.doc_type in SETTLE_DOC_TYPES],
        pay=[d for d in docs if d.doc_type in PAY_DOC_TYPES],
        anchors=[d for d in docs if bundle_service.is_contract_role(d.role)],
    )


def match_row(docs: list[DocRecord], row: LedgerRow) -> Matches:
    """台账行 → 档案库证据。编号通道优先，其次 名称互含+金额。"""
    matched = _match_by_variants(docs, contract_variants(row.contract_no))
    if not matched:
        matched = _match_by_name(docs, row.customer, row.amount)
    return _split(matched)


def _match_by_variants(docs: list[DocRecord], variants: set[str]) -> list[DocRecord]:
    return [d for d in docs if variants & d.variants] if variants else []


def match_contract(docs: list[DocRecord], contract_no: str) -> Matches:
    """按合同编号取证据（自对账/差异复核用，不做名称兜底）。"""
    return _split(_match_by_variants(docs, contract_variants(contract_no)))


# ---------------- Matcher ----------------

def diff_key(contract_no: str, dtype: str, doc_id: int | None) -> str:
    return f"{norm_no(contract_no) or '-'}|{dtype}|{doc_id or '-'}"


@dataclass
class CheckRow:
    """一条待核对的业务。台账模式来自 LedgerRow，自对账模式来自合同锚点。"""
    contract_no: str = ""
    customer: str = ""
    amount: float | None = None
    sign_date: str = ""
    settlement_total: float | None = None
    received_total: float | None = None
    our_entity: str = ""
    due_total: float | None = None      # 该收/该付：已结算额优先，退回合同金额
    direction: str = ""                 # sale | purchase | mixed | ""


SEVERE_REQUIRED = ("发票", "结算单")
PAYMENT_DIFF_TYPES = ("missing_payment", "payment_mismatch", "payment_shortfall")
RECHECKABLE = ("missing_doc", "missing_required", "amount_mismatch", "missing_payment",
               "date_suspect", "seal_missing", "payment_mismatch", "payment_shortfall")


def _emit(db: Session, run: ReconRun, counts: dict, carried: dict, diff: ReconDiff) -> None:
    """入库一条差异：命中上一期已处理的 key 就结转标记。"""
    old = carried.get(diff.key)
    if old:
        diff.resolved = old.resolved
        diff.resolution_note = old.resolution_note
    db.add(diff)
    counts[diff.diff_type] = counts.get(diff.diff_type, 0) + 1


def bundle_for_contract(db: Session, contract_no: str) -> Bundle | None:
    """按合同编号找业务包（证据抽屉用它取整包上下文）。

    先精确匹配包的主合同号，再退到条目模糊匹配：纯数字变体（"20266102"）会撞上
    别的编号写法，抽屉里挂错包比挂不上包更糟。
    """
    key = norm_no(contract_no)
    if key:
        b = db.query(Bundle).filter(Bundle.contract_key == key).first()
        if b:
            return b
    variants = contract_variants(contract_no)
    if variants:
        for item in (db.query(BundleItem).order_by(BundleItem.bundle_id, BundleItem.id).all()):
            if variants & contract_variants(item.contract_no):
                return db.get(Bundle, item.bundle_id)
    return None


def bundle_variants(db: Session, bundle: Bundle) -> set:
    """一个业务包覆盖到的合同号变体：主合同号加上包内条目的各种写法。"""
    out = set(contract_variants(bundle.contract_key))
    for item in db.query(BundleItem).filter(BundleItem.bundle_id == bundle.id).all():
        out |= contract_variants(item.contract_no)
    return out


def diff_bundle(db: Session, diff: ReconDiff) -> Bundle | None:
    """差异归属的业务包。

    detail.bundle_id 只是跑对账那一刻的快照，而「重建归集」和合并都会删掉重建自动包，
    SQLite 又会复用空出来的主键：光按 id 取包会把差异挂到别的业务上。
    所以先拿合同号验一遍 id 还对不对，对不上就按合同号重新定位。
    """
    variants = contract_variants(diff.contract_no)
    bid = (diff.detail or {}).get("bundle_id")
    if bid:
        bundle = db.get(Bundle, int(bid))
        if bundle is not None and (not variants or variants & bundle_variants(db, bundle)):
            return bundle
    return bundle_for_contract(db, diff.contract_no)


def worst_amount_bucket(m: Matches, expected: float | None) -> tuple[str, float, list[DocRecord]] | None:
    """金额比对按单据类型分桶，取偏差最大的一桶。

    发票合计与结算单合计都各自等于合同金额，把它们加在一起比必然翻倍，
    所以只能逐类推：哪一类的合计对不上合同金额，就拿哪一类的单据当依据。
    """
    if expected is None:
        return None
    buckets: dict[str, list[DocRecord]] = {}
    for d in m.settle:
        buckets.setdefault(d.doc_type or "其他", []).append(d)
    worst: tuple[str, float, list[DocRecord]] | None = None
    worst_gap = 0.0
    for dtype, ds in buckets.items():
        total = sum(d.amount or 0 for d in ds)
        if not total or amount_close(total, expected):
            continue
        gap = abs(total - expected)
        if gap > worst_gap:
            worst, worst_gap = (dtype, total, ds), gap
    return worst


def _payment_state(*, due: float | None, paid_ledger: float | None,
                   pay_docs: list["DocRecord"], direction: str, ledger_mode: bool) -> dict:
    """回款核对的唯一算式：该收付 → 已收付 → 差额，state ∈ ok|no_payment|ledger_paid|mismatch|shortfall。

    缺凭证族与金额族互斥：一张凭证都没有时只谈「缺凭证」，有凭证才谈「账证不符 / 收付不足」，
    同一件事绝不报两条；账证都不一致时先修账证，差额留到一致了再说。
    """
    voucher, due_word, paid_word, short_word, money_word = _pay_words(direction)
    paid_docs = sum(d.amount or 0 for d in pay_docs)
    out = {"state": "ok", "voucher": voucher, "expected": "", "actual": "",
           "gap": None, "docs": [d.id for d in pay_docs], "severity": "warn"}
    if not due or due <= 0:
        return out                      # 没有「该收付」的依据（只有合同、没有结算），不报回款差异
    effective = max(paid_docs, paid_ledger or 0.0)

    if not pay_docs:
        if paid_ledger:
            out["state"] = "ledger_paid"
            out["expected"] = f"台账已记{money_word} {paid_ledger:.2f}"
            out["actual"] = "档案库无凭证"
        else:
            out["state"] = "no_payment"
            out["expected"] = f"{due:.2f}"
            out["actual"] = f"未{money_word}且无凭证"
        return out

    if ledger_mode and paid_ledger and not pay_close(paid_docs, paid_ledger):
        out["state"] = "mismatch"
        out["expected"] = f"台账{paid_word} {paid_ledger:.2f}"
        out["actual"] = f"凭证合计 {paid_docs:.2f}"
        out["gap"] = round(paid_docs - paid_ledger, 2)
        return out

    if not pay_close(effective, due):
        gap = round(effective - due, 2)
        tail = f"{short_word} {-gap:.2f}" if gap < 0 else f"超出{due_word} {gap:.2f}"
        out["state"] = "shortfall"
        out["expected"] = f"{due_word} {due:.2f}"
        out["actual"] = f"{paid_word} {effective:.2f}（{tail}）"
        out["gap"] = gap
    return out


def _payment_checks(db: Session, run: ReconRun, counts: dict, carried: dict, *, row: CheckRow,
                    m: Matches, bundle_id: int | None, direction: str) -> None:
    """与 _required_diffs 平级：台账模式比账面已收付，自对账模式比包内凭证合计。"""
    due = row.due_total if row.due_total and row.due_total > 0 else None
    if due is None:
        return
    if not (m.anchors or m.settle or m.pay):
        # 档案库里连一份单据都没有：只报 missing_doc，不能凭空追一笔「该收没收到」
        return
    paid_ledger = row.received_total if row.received_total and row.received_total > 0 else None
    ledger_mode = run.source == "ledger"
    st = _payment_state(due=due, paid_ledger=paid_ledger, pay_docs=m.pay,
                        direction=direction, ledger_mode=ledger_mode)
    if st["state"] == "ok":
        return
    dtype = ("missing_payment" if st["state"] in ("no_payment", "ledger_paid")
             else "payment_mismatch" if st["state"] == "mismatch" else "payment_shortfall")
    _emit(db, run, counts, carried, ReconDiff(
        run_id=run.id, key=diff_key(row.contract_no, dtype, None), diff_type=dtype,
        severity=st["severity"], contract_no=row.contract_no, customer=row.customer,
        expected=st["expected"][:200], actual=st["actual"][:200],
        detail={"bundle_id": bundle_id, "docs": st["docs"], "due": due,
                "paid_ledger": paid_ledger, "direction": direction, "gap": st["gap"],
                "mode": "ledger" if ledger_mode else "archive", "reason": st["state"]}))


def _required_diffs(db: Session, run: ReconRun, counts: dict, carried: dict, *, row: CheckRow,
                    m: Matches, reqs: dict, bundle_id: int | None) -> None:
    """必备单据清单 → 逐角色缺件差异（role 写进 detail，前端据此出上传入口）。"""
    seen: set[str] = set()
    for role in sorted({d.role for d in m.anchors}):
        for r in reqs.get(role, []):
            if r.required_role in seen:
                continue
            seen.add(r.required_role)
            got = m.of_role(r.required_role)
            if len(got) >= (r.min_count or 1):
                continue
            _emit(db, run, counts, carried, ReconDiff(
                run_id=run.id,
                key=diff_key(row.contract_no, f"missing_required:{r.required_role}", None),
                diff_type="missing_required",
                severity="error" if r.required_role in SEVERE_REQUIRED else "warn",
                contract_no=row.contract_no, customer=row.customer,
                expected=f"至少 {r.min_count or 1} 份{r.required_role}",
                actual=f"已归档 {len(got)} 份",
                detail={"required_role": r.required_role, "min_count": r.min_count or 1,
                        "amount_check": bool(r.amount_check), "bundle_id": bundle_id,
                        "docs": [d.id for d in got]}))


def _check_row(db: Session, run: ReconRun, counts: dict, carried: dict, row: CheckRow,
               m: Matches, reqs: dict) -> None:
    """一份业务的全部检查项；台账模式与自对账模式共用，保证口径一致。"""
    b = bundle_for_contract(db, row.contract_no)
    bundle_id = b.id if b else None
    direction = _norm_direction(row.direction) or (b.direction if b else "")

    if m.anchors:
        # 有合同锚点：按清单报缺件，不再重复报粗粒度 missing_doc
        _required_diffs(db, run, counts, carried, row=row, m=m, reqs=reqs, bundle_id=bundle_id)
    elif not m.settle:
        # 连合同本身都没归档：只能粗粒度提醒，且不能要求附件
        _emit(db, run, counts, carried, ReconDiff(
            run_id=run.id, key=diff_key(row.contract_no, "missing_doc", None),
            diff_type="missing_doc", severity="error", contract_no=row.contract_no,
            customer=row.customer, expected="结算/发票单据", actual="未找到匹配归档文档",
            detail={"bundle_id": bundle_id, "docs": [], "hint": "档案库中没有该合同的任何单据"}))

    if m.settle:
        gap = worst_amount_bucket(m, row.amount)
        if gap:
            dtype, total, ds = gap
            _emit(db, run, counts, carried, ReconDiff(
                run_id=run.id, key=diff_key(row.contract_no, "amount_mismatch", None),
                diff_type="amount_mismatch", severity="warn", contract_no=row.contract_no,
                customer=row.customer, expected=f"{row.amount:.2f}", actual=f"{total:.2f}",
                detail={"docs": [d.id for d in ds], "files": [d.filename for d in ds],
                        "doc_type": dtype, "bundle_id": bundle_id}))

    for d in m.settle:
        if d.doc_date and row.sign_date and d.doc_date < row.sign_date:
            _emit(db, run, counts, carried, ReconDiff(
                run_id=run.id, key=diff_key(row.contract_no, "date_suspect", d.id),
                diff_type="date_suspect", severity="warn", contract_no=row.contract_no,
                customer=row.customer, document_id=d.id, expected=f"晚于签订日 {row.sign_date}",
                actual=f"单据日期 {d.doc_date}",
                detail={"file": d.filename, "sign_date": row.sign_date, "bundle_id": bundle_id}))

    # 盖章只讲合同本身：旧代码在结算类文档里找 doc_type=="合同"，永远命中不了
    for d in m.anchors:
        if d.has_seal is False:
            _emit(db, run, counts, carried, ReconDiff(
                run_id=run.id, key=diff_key(row.contract_no, "seal_missing", d.id),
                diff_type="seal_missing", severity="info", contract_no=row.contract_no,
                customer=row.customer, document_id=d.id, expected="盖章件",
                actual="未检测到红章", detail={"file": d.filename, "bundle_id": bundle_id}))

    _payment_checks(db, run, counts, carried, row=row, m=m, bundle_id=bundle_id,
                    direction=direction)


def _checkrow_from_ledger(r: LedgerRow) -> CheckRow:
    """台账行 → 待核对业务；`due_total` = 已结算额优先，退回合同金额。"""
    return CheckRow(contract_no=r.contract_no, customer=r.customer, amount=r.amount,
                    sign_date=r.sign_date, settlement_total=r.settlement_total,
                    received_total=r.received_total, our_entity=r.our_entity,
                    due_total=r.settlement_total if r.settlement_total and r.settlement_total > 0
                    else r.amount,
                    direction=_norm_direction(r.direction))


def _archive_rows(db: Session) -> list[CheckRow]:
    """自对账：以每份已归档合同为锚，一行 = 一个业务包里的一个合同号。"""
    reqs = bundle_service.requirements_map(db)
    rows: list[CheckRow] = []
    seen: set[str] = set()
    for b in db.query(Bundle).order_by(Bundle.id).all():
        rep = bundle_service.bundle_report(db, b, reqs, None)
        for g in rep["groups"]:
            if not [a for a in g["anchors"] if bundle_service.is_contract_role(a["role"])]:
                continue
            key = norm_no(g["contract_no"]) or f"bundle-{b.id}"
            if key in seen:
                continue
            seen.add(key)
            # 该收付 = 包内结算/发票分桶合计（结算了就该收到钱），没有结算类单据退回合同金额
            rows.append(CheckRow(contract_no=g["contract_no"], customer=rep["customer"],
                                 amount=g["amount"], sign_date=g["sign_date"],
                                 our_entity=rep["our_entity"],
                                 due_total=g["settlement_sum"] or g["amount"],
                                 direction=rep["direction"]))
    return rows


def _resolve_source(db: Session, source: str) -> str:
    src = (source or "auto").strip().lower()
    has_ledger = db.query(Ledger).count() > 0
    if src == "ledger":
        if not has_ledger:
            raise ValueError("选择台账对账但还没有台账，请先上传业务台账 Excel")
        return "ledger"
    if src == "archive":
        return "archive"
    return "ledger" if has_ledger else "archive"


def run_reconciliation(db: Session, ledger_id: int | None = None,
                       source: str = "auto") -> ReconRun:
    mode = _resolve_source(db, source)
    docs = build_doc_index(db)

    ledgers: list[Ledger] = []
    rows: list = []
    if mode == "ledger":
        q = db.query(Ledger)
        if ledger_id:
            q = q.filter(Ledger.id == ledger_id)
        ledgers = q.all()
        if not ledgers:
            raise ValueError("还没有台账，请先上传业务台账 Excel")
        rows = [_checkrow_from_ledger(r) for l in ledgers for r in l.rows]
    else:
        rows = _archive_rows(db)

    run = ReconRun(ledger_id=ledgers[0].id if ledgers else None, status="running",
                   total_rows=len(rows), total_docs=len(docs), source=mode)
    db.add(run)
    db.flush()

    # 结转上一期“已处理”标记：同 key 的差异不再重复打扰
    prev_run = (db.query(ReconRun).filter(ReconRun.status == "completed", ReconRun.id != run.id)
                .order_by(ReconRun.id.desc()).first())
    carried = {d.key: d for d in (prev_run.diffs if prev_run else []) if d.resolved}
    counts: dict[str, int] = {}
    reqs = bundle_service.requirements_map(db)

    if mode == "ledger":
        known: set[str] = set()
        used: set[int] = set()
        for row in rows:
            m = match_row(docs, row)
            used |= m.doc_ids
            known |= contract_variants(row.contract_no)
            _check_row(db, run, counts, carried, row, m, reqs)
        for d in docs:
            if d.contract_no and d.id not in used and not (d.variants & known):
                _emit(db, run, counts, carried, ReconDiff(
                    run_id=run.id, key=diff_key(d.contract_no, "extra_doc", d.id),
                    diff_type="extra_doc", severity="warn", contract_no=d.contract_no,
                    customer=d.customer, document_id=d.id,
                    expected="台账中应存在该合同编号", actual=f"文档 {d.filename} 无对应台账",
                    detail={"file": d.filename}))
    else:
        for row in rows:
            _check_row(db, run, counts, carried, row, match_contract(docs, row.contract_no), reqs)

    run.counts = counts
    run.status = "completed"
    run.finished_at = datetime.now()
    db.commit()
    logger.info("对账完成 run#%s source=%s 差异 %s", run.id, mode, counts)
    return run


def diffs_query(db: Session, run_id: int, only_open: bool = False, diff_type: str | None = None):
    q = db.query(ReconDiff).filter(ReconDiff.run_id == run_id)
    if only_open:
        q = q.filter(ReconDiff.resolved.is_(False))
    if diff_type:
        q = q.filter(ReconDiff.diff_type == diff_type)
    return q.order_by(ReconDiff.severity.desc(), ReconDiff.id).all()


def resolve_diff(db: Session, diff: ReconDiff, note: str = "") -> None:
    diff.resolved = True
    diff.resolution_note = note[:500]
    db.commit()
    recount_run(db, run_of(db, diff))


def run_of(db: Session, diff: ReconDiff) -> ReconRun | None:
    """差异所属的对账期；resolve/recheck 之后用它重算 counts。"""
    return db.get(ReconRun, diff.run_id)


def recount_run(db: Session, run: ReconRun | None) -> dict:
    """按当前差异重算本期 counts：KPI 只统计未处理的，和界面所见一致。"""
    if not run:
        return {}
    counts: dict[str, int] = {}
    for d in run.diffs:
        if d.resolved:
            continue
        counts[d.diff_type] = counts.get(d.diff_type, 0) + 1
    run.counts = counts
    db.commit()
    return counts


def _doc_names(db: Session, ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    from backend.models import Document

    return {d.id: (d.current_filename or d.original_filename)
            for d in db.query(Document).filter(Document.id.in_(ids)).all()}


def _closed_note(ids: list[int], names: dict[int, str], label: str) -> str:
    if not ids:
        return f"{label}已补齐"
    head = ids[:2]
    txt = "、".join(f"#{i}《{names.get(i, '?')}》" for i in head)
    if len(ids) > 2:
        txt += f" 等 {len(ids)} 份"
    return f"由文档 {txt} 自动补齐{label}"


def recheck_diff(db: Session, diff: ReconDiff) -> dict:
    """就地复核：条件满足就自动关闭并留痕文档 id，不满足则写明仍缺什么。"""
    if diff.resolved:
        detail = diff.detail or {}
        return {"resolved": True, "changed": False, "remaining": [],
                "note": diff.resolution_note, "closed_by": detail.get("closed_by") or []}

    docs = build_doc_index(db)
    m = match_contract(docs, diff.contract_no)
    detail = dict(diff.detail or {})
    closed_by: list[int] = []
    remaining: list[str] = []
    label = ""

    if diff.diff_type == "missing_required":
        role = detail.get("required_role") or ""
        need = int(detail.get("min_count") or 1)
        before = {int(x) for x in (detail.get("docs") or [])}
        got = m.of_role(role)
        label = role
        if len(got) >= need:
            closed_by = [d.id for d in got if d.id not in before] or [d.id for d in got]
        else:
            remaining = [role]
    elif diff.diff_type == "missing_doc":
        got = m.settle or m.anchors
        label = "单据"
        if got:
            closed_by = [d.id for d in got][:3]
        else:
            remaining = ["单据"]
    elif diff.diff_type == "amount_mismatch":
        # 与 worst_amount_bucket 同一口径：只和当初报错的那一类单据比，别把发票+结算叠一起
        expected = to_decimal(diff.expected)
        bucket = detail.get("doc_type") or ""
        group = [d for d in m.settle if (d.doc_type or "其他") == bucket] if bucket else list(m.settle)
        total = sum(d.amount or 0 for d in group)
        label = bucket or "单据"
        if not group or amount_close(total, expected):
            before = {int(x) for x in (detail.get("docs") or [])}
            closed_by = [d.id for d in group if d.id not in before]
            if not closed_by:
                remaining = []
        else:
            remaining = ["金额"]
    elif diff.diff_type in PAYMENT_DIFF_TYPES:
        # 用与首轮同一套算式复算：凭证出现 → 账证一致 → 差额进入容差，逐层放行
        st = _payment_state(due=detail.get("due"), paid_ledger=detail.get("paid_ledger"),
                            pay_docs=m.pay, direction=detail.get("direction") or "",
                            ledger_mode=detail.get("mode") == "ledger")
        label = st["voucher"]
        if st["state"] == "ok":
            before = {int(x) for x in (detail.get("docs") or [])}
            closed_by = [d.id for d in m.pay if d.id not in before] or [d.id for d in m.pay]
            if not closed_by:
                closed_by = sorted(before)
        else:
            remaining = [st["voucher"]]
            detail["reason"] = st["state"]
            detail["gap"] = st["gap"]
            detail["docs"] = st["docs"]
            diff.expected = st["expected"][:200]
            diff.actual = st["actual"][:200]
    elif diff.diff_type == "date_suspect":
        rec = next((d for d in docs if d.id == diff.document_id), None)
        sign = detail.get("sign_date") or ""
        if rec is None:
            remaining = ["单据"]
        elif not sign or not rec.doc_date or rec.doc_date >= sign:
            closed_by = [rec.id]
            label = "日期"
        else:
            remaining = ["日期"]
    elif diff.diff_type == "seal_missing":
        from backend.models import Document

        doc = db.get(Document, diff.document_id) if diff.document_id else None
        if doc is None or doc.has_seal:
            closed_by = [diff.document_id] if doc else []
            label = "盖章件"
        else:
            remaining = ["盖章件"]
    else:
        return {"resolved": False, "changed": False, "remaining": [],
                "note": "该类型需人工处理", "closed_by": []}

    names = _doc_names(db, closed_by)
    if closed_by:
        diff.resolved = True
        diff.resolution_note = _closed_note(closed_by, names, label)[:500]
        detail["closed_by"] = closed_by
        detail.pop("still_missing", None)
    else:
        detail["still_missing"] = remaining
    diff.detail = detail
    db.commit()
    recount_run(db, run_of(db, diff))
    return {"resolved": bool(diff.resolved), "changed": bool(closed_by),
            "remaining": remaining, "note": diff.resolution_note if closed_by else "",
            "closed_by": closed_by}


def diff_evidence(db: Session, diff: ReconDiff) -> dict:
    """一条差异的依据：所属业务包 + 已匹配文档 + 仍缺项（含上传入口所需信息）。"""
    run = run_of(db, diff)
    detail = diff.detail or {}
    bundle = diff_bundle(db, diff)

    rep = bundle_service.bundle_report(db, bundle) if bundle else None
    group = None
    if rep:
        want = contract_variants(diff.contract_no)
        for g in rep["groups"]:
            if want & contract_variants(g["contract_no"]):
                group = g
                break
        if group is None and len(rep["groups"]) == 1:
            group = rep["groups"][0]

    matched = (group["anchors"] + group["attachments"]) if group else []
    for row in matched:
        row["is_anchor"] = bundle_service.is_contract_role(row["role"])
    missing = [q for q in (group["required"] if group else []) if not q["ok"]]
    role = detail.get("required_role") or ""
    if group is None and role:
        # 没有包上下文时，只能按差异自己记录的缺项类型显示
        missing = [{"role": role, "present": 0, "min_count": detail.get("min_count") or 1,
                    "ok": False, "amount_check": detail.get("amount_check", True),
                    "expected": None, "actual": None, "amount_ok": None}]

    payment = None
    if diff.diff_type in PAYMENT_DIFF_TYPES:
        voucher, due_word, paid_word, short_word, _money = _pay_words(detail.get("direction") or "")
        due = detail.get("due")
        pay_rows = [d for d in (group["attachments"] if group else []) if d["role"] in PAY_DOC_TYPES]
        paid = sum(d.get("amount") or 0 for d in pay_rows)
        if not group and detail.get("docs"):
            ids = {int(x) for x in detail["docs"] if str(x).lstrip("-").isdigit()}
            paid = sum(d.amount or 0 for d in build_doc_index(db) if d.id in ids)
        paid_ledger = detail.get("paid_ledger")
        effective = max(paid, paid_ledger or 0.0)
        payment = {"due": due, "paid": paid, "paid_ledger": paid_ledger, "effective": effective,
                   "outstanding": round(due - effective, 2) if due else None,
                   "uncleared": round(due - paid, 2) if due else None,
                   "voucher": voucher, "due_word": due_word, "paid_word": paid_word,
                   "short_word": short_word, "reason": detail.get("reason") or ""}
        if not any(q.get("role") in PAY_DOC_TYPES for q in missing):
            missing = list(missing) + [{
                "role": "银行回单", "present": len(pay_rows), "min_count": 1, "ok": False,
                "amount_check": True, "expected": due, "actual": paid, "amount_ok": False,
                "label": voucher}]

    return {
        "diff": {"id": diff.id, "type": diff.diff_type,
                 "label": DIFF_TYPE_LABEL.get(diff.diff_type, diff.diff_type),
                 "role": role, "severity": diff.severity, "contract_no": diff.contract_no,
                 "customer": diff.customer, "expected": diff.expected, "actual": diff.actual,
                 "resolved": diff.resolved, "resolution_note": diff.resolution_note,
                 "detail": detail},
        "run": {"id": run.id if run else None, "source": run.source if run else "ledger",
                "status": run.status if run else ""},
        "bundle": ({k: v for k, v in rep.items() if k != "groups"} if rep else None),
        "bundle_id": bundle.id if bundle else None,
        "group": group,
        "payment": payment,
        "docs": matched,
        "missing": missing,
        "still_missing": detail.get("still_missing") or [],
    }


DIFF_TYPE_LABEL = {
    "missing_doc": "缺结算/发票单据",
    "missing_required": "缺必备单据",
    "missing_payment": "未回款/缺凭证",
    "payment_shortfall": "收付金额不符",
    "payment_mismatch": "凭证与台账不符",
    "amount_mismatch": "金额不符",
    "extra_doc": "文档无对应台账",
    "date_suspect": "日期可疑",
    "seal_missing": "合同疑似缺章",
}


def export_diffs_csv(db: Session, run_id: int) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["类型", "严重度", "合同编号", "客户", "期望", "实际", "文档ID", "状态", "备注"])
    for d in diffs_query(db, run_id):
        w.writerow([DIFF_TYPE_LABEL.get(d.diff_type, d.diff_type), d.severity, d.contract_no,
                    d.customer, d.expected, d.actual, d.document_id or "",
                    "已处理" if d.resolved else "未处理", d.resolution_note])
    return buf.getvalue().encode("utf-8-sig")


def export_diffs_xlsx(db: Session, run_id: int) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "差异明细"
    ws.append(["类型", "严重度", "合同编号", "客户", "期望", "实际", "文档ID", "状态", "备注"])
    for d in diffs_query(db, run_id):
        ws.append([DIFF_TYPE_LABEL.get(d.diff_type, d.diff_type), d.severity, d.contract_no,
                   d.customer, d.expected, d.actual, d.document_id or "",
                   "已处理" if d.resolved else "未处理", d.resolution_note])
    run = db.get(ReconRun, run_id)
    ws2 = wb.create_sheet("汇总")
    ws2.append(["差异类型", "数量"])
    for k, v in (run.counts or {}).items() if run else []:
        ws2.append([DIFF_TYPE_LABEL.get(k, k), v])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# ---------------- 定时巡检 ----------------

_thread: threading.Thread | None = None
_stop = threading.Event()
_status: dict = {"running": False, "last_run": None, "last_run_id": None, "error": ""}


def _loop() -> None:
    from backend.database import SessionLocal

    while not _stop.is_set():
        try:
            with SessionLocal() as db:
                run = run_reconciliation(db)
                _status.update(last_run=datetime.now().isoformat(timespec="seconds"),
                               last_run_id=run.id, error="")
        except Exception as e:  # noqa: BLE001
            logger.warning("定时对账失败: %s", e)
            _status["error"] = str(e)
        _stop.wait(max(10, settings.RECON_INTERVAL_MINUTES) * 60)
    _status["running"] = False


def sync_scheduler() -> bool:
    global _thread, _stop
    if settings.RECON_ENABLED:
        if not (_thread and _thread.is_alive()):
            _stop = threading.Event()
            _thread = threading.Thread(target=_loop, name="recon-scheduler", daemon=True)
            _status["running"] = True
            _thread.start()
        return True
    _stop.set()
    return False