"""付款/回款数据参与对账：该收付 → 已收付 → 差额，台账与档案库互为证据。"""
from __future__ import annotations

import time

import openpyxl

from backend.config import settings
from backend.models import ReconDiff
from backend.services import bundles as bsvc
from backend.services import reconciliation as svc
from tests.test_bundles import _cat, _doc, _invoice, _pickup, _sale, _settle

PAY_TYPES = ("missing_payment", "payment_mismatch", "payment_shortfall")
CUSTOMER = "北方钢铁贸易有限公司"
OURS = "华东供应链有限公司"


def _pay(db, n, cno, amount, date="2026-04-10"):
    """一张银行回单：角色与上传占位类型同名。"""
    return _doc(db, f"回单{n}.txt", "银行回单", "银行回单", cno=cno, amount=amount, date=date,
                party=CUSTOMER, company=OURS)


def _contract_xlsx(db, cno, name, amount=350000):
    p = settings.ledger_root / f"{name}.xlsx"
    p.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "合同台账"
    ws.append(["合同编号", "客户名称", "合同金额", "签订日期", "业务类别", "我方主体"])
    ws.append([cno, CUSTOMER, amount, "2026-03-05", "销售", OURS])
    wb.save(p)
    return p


def _ledger2(db, contracts, payments, name):
    """合同表 + 回款表（列名刻意用「付款金额/到账日期」，考察别名是否读得进来）。"""
    p = settings.ledger_root / f"{name}.xlsx"
    p.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "合同台账"
    ws.append(["合同编号", "客户名称", "合同金额", "签订日期", "业务类别", "我方主体"])
    for row in contracts:
        ws.append(list(row))
    w2 = wb.create_sheet("回款表")
    w2.append(["合同编号", "客户名称", "付款金额", "到账日期"])
    for row in payments:
        w2.append(list(row))
    wb.save(p)
    parsed = svc.read_ledger_file(p)
    ledger = svc.save_ledger(db, name, str(p), parsed)
    db.expire_all()
    return parsed, ledger


def _one(run, dtype):
    got = [d for d in run.diffs if d.diff_type == dtype]
    assert len(got) == 1, [(d.diff_type, d.detail) for d in run.diffs]
    return got[0]


def _pay_diffs(run):
    return [d for d in run.diffs if d.diff_type in PAY_TYPES]


def test_payment_sheet_alias_reads_amount(db):
    """回款表列名叫「付款金额/到账日期」也要读出金额，否则付款对账永远拿不到数。"""
    cno = "XS-2026-9001"
    parsed, ledger = _ledger2(db, [(cno, CUSTOMER, 500000, "2026-03-05", "销售", OURS)],
                              [[cno, CUSTOMER, 200000, "2026-04-10"]], "回款台账9001")
    assert parsed["sheets"]["回款表"] == "payment"
    assert parsed["payment"][0]["amount"] == 200000.0
    assert parsed["payment"][0]["date"] == "2026-04-10"
    row = db.query(svc.LedgerRow).filter(svc.LedgerRow.ledger_id == ledger.id).first()
    assert row.received_total == 200000.0
    cr = svc._checkrow_from_ledger(row)
    assert cr.due_total == 500000.0 and cr.direction == "sale"


def test_ledger_paid_but_no_voucher_then_upload_closes(db, client):
    """台账说收到钱了、档案库却没凭证 → ledger_paid；补一张回单即自动关闭。"""
    cno = "XS-2026-9002"
    _, ledger = _ledger2(db, [(cno, CUSTOMER, 350000, "2026-03-05", "销售", OURS)],
                         [[cno, CUSTOMER, 350000, "2026-04-12"]], "回款台账9002")
    _sale(db, "9002", cno)
    _invoice(db, "9002", cno)
    _settle(db, "9002", cno)
    _pickup(db, "9002", cno)

    run = svc.run_reconciliation(db, ledger.id, "ledger")
    d = _one(run, "missing_payment")
    assert d.severity == "warn"
    assert d.detail["reason"] == "ledger_paid"
    assert d.detail["paid_ledger"] == 350000.0 and d.detail["due"] == 350000.0
    assert "台账已记回款" in d.expected and d.actual == "档案库无凭证"

    body = ("中国工商银行电子回单\n"
            f"回单编号：202604120001\n合同编号：{cno}\n金额：¥350,000.00\n"
            f"收款人：{OURS}\n付款人：{CUSTOMER}")
    r = client.post(f"/api/reconciliation/diffs/{d.id}/upload-doc",
                    files={"files": ("银行回单9002.txt", body.encode("utf-8"), "text/plain")})
    assert r.status_code == 200, r.text
    up = r.json()
    for _ in range(250):
        if client.get(f"/api/processing/jobs/{up['job_id']}").json()["status"] != "running":
            break
        time.sleep(0.1)
    db.expire_all()
    doc = db.query(svc.Document).filter(svc.Document.original_filename.like("%回单9002%")).first()
    assert doc, "上传的回单没进档案库"
    if doc.status != "archived":
        r = client.post(f"/api/review/{doc.id}/approve",
                        json={"category_id": _cat(db, "银行回单").id, "document_type": "银行回单"})
        assert r.status_code == 200, r.text

    out = client.post(f"/api/reconciliation/diffs/{d.id}/recheck").json()
    assert out["resolved"] is True and out["changed"] is True
    assert out["closed_by"] == [doc.id]
    assert f"#{doc.id}" in out["note"] and "回款凭证" in out["note"]
    db.expire_all()
    fresh = db.get(ReconDiff, d.id)
    assert fresh.resolved and "missing_payment" not in (svc.run_of(db, fresh).counts or {})


def test_no_money_and_no_voucher_is_no_payment(db):
    """台账没记回款、档案库也没凭证 → no_payment，expected 就是要收的钱。"""
    cno = "XS-2026-9003"
    p = _contract_xlsx(db, cno, "仅合同9003")
    ledger = svc.save_ledger(db, "仅合同台账9003", str(p), svc.read_ledger_file(p))
    _sale(db, "9003", cno)
    _invoice(db, "9003", cno)
    _settle(db, "9003", cno)
    run = svc.run_reconciliation(db, ledger.id, "ledger")
    d = _one(run, "missing_payment")
    assert d.detail["reason"] == "no_payment"
    assert d.detail["paid_ledger"] is None
    assert d.expected == "350000.00" and d.actual == "未回款且无凭证"


def test_voucher_vs_ledger_mismatch_and_tolerance(db):
    """凭证与台账差 3% 报 payment_mismatch；差 0.5% 在容差内不报。"""
    cno = "XS-2026-9004"
    _, ledger = _ledger2(db, [(cno, CUSTOMER, 400000, "2026-03-05", "销售", OURS)],
                         [[cno, CUSTOMER, 400000, "2026-04-12"]], "回款台账9004")
    _sale(db, "9004", cno, amount=400000.0)
    _invoice(db, "9004", cno, amount=400000.0)
    _settle(db, "9004", cno, amount=400000.0)
    _pickup(db, "9004", cno, amount=400000.0)
    _pay(db, "9004", cno, 388000.0)      # 比台账少 3%

    run = svc.run_reconciliation(db, ledger.id, "ledger")
    d = _one(run, "payment_mismatch")
    assert d.severity == "warn"
    assert d.detail["gap"] == -12000.0
    assert "台账已收" in d.expected and "凭证合计" in d.actual
    assert d.detail["docs"], "差异必须带上凭证文档 id，抽屉才出缩略图"
    # 账证都没对上之前不谈差额，同一件事只报一条
    assert not [x for x in run.diffs if x.diff_type == "payment_shortfall"]

    _pay(db, "9004B", cno, 10000.0)      # 补上尾差 → 398,000，与台账只差 0.5%，落在容差内
    run2 = svc.run_reconciliation(db, ledger.id, "ledger")
    assert not _pay_diffs(run2), [(d.diff_type, d.actual) for d in run2.diffs]


def test_archive_self_recon_shortfall(db):
    """自对账：结算 100 万、只收到 60 万 → 差额 -40 万；补齐后不再报回款差异。"""
    cno = "XS-2026-9005"
    _sale(db, "9005", cno, amount=1000000.0)
    _invoice(db, "9005", cno, amount=1000000.0)
    _settle(db, "9005", cno, amount=1000000.0)
    _pickup(db, "9005", cno, amount=1000000.0)
    _pay(db, "9005", cno, 600000.0)

    run = svc.run_reconciliation(db, None, "archive")
    d = _one(run, "payment_shortfall")
    assert d.detail["mode"] == "archive" and d.detail["paid_ledger"] is None
    assert d.detail["gap"] == -400000.0
    assert d.expected == "应收 1000000.00" and "已收 600000.00" in d.actual
    assert "未收 400000.00" in d.actual

    _pay(db, "9005B", cno, 400000.0)
    run2 = svc.run_reconciliation(db, None, "archive")
    assert not _pay_diffs(run2), [(x.diff_type, x.actual) for x in run2.diffs]


def test_purchase_wording_and_mixed(db):
    """采购侧文案换成付款/已付/未付，类型名不变；方向不明时走收付文案。"""
    cno = "CG-2026-9006"
    _doc(db, "采购合同9006.txt", "合同", "采购合同", cno=cno, amount=800000.0,
         date="2026-03-01", party=CUSTOMER, company=OURS)
    _invoice(db, "9006", cno, amount=800000.0)
    _settle(db, "9006", cno, amount=800000.0)
    _pay(db, "9006", cno, 500000.0)

    run = svc.run_reconciliation(db, None, "archive")
    d = _one(run, "payment_shortfall")
    assert "应付" in d.expected and "已付" in d.actual and "未付 300000.00" in d.actual
    assert d.detail["direction"] == "purchase"
    assert svc.DIFF_TYPE_LABEL[d.diff_type] == "收付金额不符"

    st = svc._payment_state(due=1000.0, paid_ledger=None, pay_docs=[], direction="mixed",
                            ledger_mode=False)
    assert st["state"] == "no_payment" and st["voucher"] == "收付凭证"
    assert st["actual"] == "未收付且无凭证"


def test_no_due_basis_reports_nothing(db):
    """只有合同、连金额都没有 → 没有任何回款类差异，不能凭空造应收。"""
    cno = "XS-2026-9007"
    _doc(db, "销售合同9007.txt", "合同", "销售合同", cno=cno, date="2026-03-05",
         party=CUSTOMER, company=OURS)
    run = svc.run_reconciliation(db, None, "archive")
    assert run.diffs, "该有缺件差异"
    assert not _pay_diffs(run), [(d.diff_type, d.detail) for d in run.diffs]


def test_unarchived_contract_has_no_payment_noise(db):
    """台账里有一条完全没归档的合同：只报缺单据，不能凭空追一笔「该收没收到」。"""
    cno = "XS-2026-9013"
    p = _contract_xlsx(db, cno, "仅合同9013", amount=120000)
    ledger = svc.save_ledger(db, "仅合同台账9013", str(p), svc.read_ledger_file(p))
    run = svc.run_reconciliation(db, ledger.id, "ledger")
    assert [d.diff_type for d in run.diffs] == ["missing_doc"]


def test_mismatch_wins_over_shortfall(db):
    """账证不符优先于差额：先修账证，别把同一笔钱报成两条。"""
    cno = "XS-2026-9008"
    _, ledger = _ledger2(db, [(cno, CUSTOMER, 900000, "2026-03-05", "销售", OURS)],
                         [[cno, CUSTOMER, 900000, "2026-04-20"]], "回款台账9008")
    _sale(db, "9008", cno)
    _invoice(db, "9008", cno)
    _settle(db, "9008", cno)
    _pickup(db, "9008", cno)
    _pay(db, "9008", cno, 300000.0)      # 凭证远小于台账：既不符也差 60 万

    run = svc.run_reconciliation(db, ledger.id, "ledger")
    assert [d.diff_type for d in _pay_diffs(run)] == ["payment_mismatch"]


def test_bundle_report_payment_view(db):
    """包上要能看到应收/已收/未收，且回款不进齐备度。"""
    cno = "XS-2026-9009"
    _sale(db, "9009", cno, amount=700000.0)
    _invoice(db, "9009", cno, amount=700000.0)
    _settle(db, "9009", cno, amount=700000.0)
    _pickup(db, "9009", cno, amount=700000.0)
    receipt = _pay(db, "9009", cno, 500000.0)
    db.expire_all()

    bundle = svc.bundle_for_contract(db, cno)
    rep = bsvc.bundle_report(db, bundle)
    g = rep["groups"][0]
    assert g["due_sum"] == 700000.0 and g["pay_sum"] == 500000.0
    assert g["pay_gap"] == 200000.0 and g["pay_state"] == "partial"
    assert [d["document_id"] for d in g["pay_docs"]] == [receipt.id]
    assert rep["payment"] == {"due": 700000.0, "paid": 500000.0, "outstanding": 200000.0,
                              "state": "partial",
                              "words": {"voucher": "回款凭证", "due": "应收", "paid": "已收",
                                        "short": "未收", "money": "回款"}}
    assert rep["completeness"] == {"have": 3, "need": 3, "missing": []}
    assert rep["complete"] is True
    # 回款不进必备清单：缺凭证只能由 missing_payment 一条负责
    assert "银行回单" not in [q["role"] for q in g["required"]]

    from backend.models import BundleItem

    it = db.query(BundleItem).filter(BundleItem.bundle_id == bundle.id,
                                     BundleItem.document_id == receipt.id).first()
    db.delete(it)
    db.commit()
    db.expire_all()
    rep2 = bsvc.bundle_report(db, bundle)
    assert rep2["payment"]["paid"] is None and rep2["payment"]["state"] == "none"
    assert rep2["completeness"] == {"have": 3, "need": 3, "missing": []}


def test_payment_tolerance_setting_takes_effect(db):
    """把回款容差调到 3% 重跑，凭证与台账的 3% 差异就不再刷屏。"""
    cno = "XS-2026-9010"
    _, ledger = _ledger2(db, [(cno, CUSTOMER, 400000, "2026-03-05", "销售", OURS)],
                         [[cno, CUSTOMER, 400000, "2026-04-12"]], "回款台账9010")
    _sale(db, "9010", cno)
    _invoice(db, "9010", cno)
    _settle(db, "9010", cno)
    _pickup(db, "9010", cno)
    _pay(db, "9010", cno, 388000.0)

    run = svc.run_reconciliation(db, ledger.id, "ledger")
    assert [d.diff_type for d in _pay_diffs(run)] == ["payment_mismatch"]

    old = settings.RECON_PAYMENT_TOLERANCE_PCT
    settings.RECON_PAYMENT_TOLERANCE_PCT = 0.03
    try:
        run2 = svc.run_reconciliation(db, ledger.id, "ledger")
        assert not _pay_diffs(run2), [(d.diff_type, d.actual) for d in run2.diffs]
    finally:
        settings.RECON_PAYMENT_TOLERANCE_PCT = old


def test_evidence_drawer_carries_payment_numbers(db):
    """抽屉要能直接看到应收/已收/未收，并把缺凭证合成一条可上传的缺项。"""
    cno = "XS-2026-9011"
    _sale(db, "9011", cno, amount=600000.0)
    _invoice(db, "9011", cno, amount=600000.0)
    _settle(db, "9011", cno, amount=600000.0)
    _pickup(db, "9011", cno, amount=600000.0)

    run = svc.run_reconciliation(db, None, "archive")
    d = _one(run, "missing_payment")
    ev = svc.diff_evidence(db, d)
    assert ev["payment"]["due"] == 600000.0 and ev["payment"]["paid"] == 0
    assert ev["payment"]["outstanding"] == 600000.0
    assert ev["payment"]["voucher"] == "回款凭证"
    roles = [q["role"] for q in ev["missing"]]
    assert "银行回单" in roles, roles
    row = [q for q in ev["missing"] if q["role"] == "银行回单"][0]
    assert row["present"] == 0 and row["min_count"] == 1 and row["expected"] == 600000.0


def test_recycling_voucher_moves_payment_back(db):
    """回单被回收 → 包上的已收回落；恢复后数字回来（回收站不该继续算钱）。"""
    from backend.models import Document, RecycleItem
    from backend.services.recycle import restore, to_recycle

    cno = "XS-2026-9012"
    _sale(db, "9012", cno, amount=480000.0)
    _invoice(db, "9012", cno, amount=480000.0)
    _settle(db, "9012", cno, amount=480000.0)
    _pickup(db, "9012", cno, amount=480000.0)
    receipt = _pay(db, "9012", cno, 300000.0)
    db.expire_all()

    bundle = svc.bundle_for_contract(db, cno)
    assert bsvc.bundle_report(db, bundle)["payment"]["paid"] == 300000.0

    to_recycle(db, db.get(Document, receipt.id))
    db.expire_all()
    rep = bsvc.bundle_report(db, bundle)
    assert rep["payment"]["paid"] is None and rep["payment"]["state"] == "none"
    assert rep["groups"][0]["pay_docs"] == []
    assert rep["complete"] is True, "回收一张回单不该把齐备度打回去"

    run = svc.run_reconciliation(db, None, "archive")
    # 回收等于凭证消失：先退回到「缺凭证」，而不是继续报差额
    assert [d.diff_type for d in _pay_diffs(run)] == ["missing_payment"]

    item = db.query(RecycleItem).filter(RecycleItem.document_id == receipt.id).first()
    restore(db, item)
    db.expire_all()
    assert bsvc.bundle_report(db, bundle)["payment"]["paid"] == 300000.0
    run2 = svc.run_reconciliation(db, None, "archive")
    assert [d.diff_type for d in _pay_diffs(run2)] == ["payment_shortfall"]
    assert _pay_diffs(run2)[0].detail["gap"] == -180000.0
