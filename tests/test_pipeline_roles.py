"""真实入库链路的类型、角色与必备单据。

手搓的 Document 行由测试自己指定分类，会掩盖「附件全被判成合同」这类问题；
这里的用例把文件真的过一遍识别→归档流水线，保证文档归集与有据对账在真实数据上也成立。
"""
from __future__ import annotations

import time
from datetime import datetime

import openpyxl

from backend.config import settings
from backend.models import Category, Document, DocumentText, space_cjk
from backend.services import bundles as bundle_service
from backend.services import reconciliation as svc

SALE = ("销售合同\n合同编号：{cno}\n甲方：北方钢铁贸易有限公司\n乙方：华东供应链有限公司\n"
        "签订日期：2026-03-05\n合同金额：¥350,000.00\n标的：热轧卷板 500 吨。")
SALE_SETTLE = "结算单\n合同编号：{cno}\n结算日期：2026-03-20\n结算金额：350000元"
SALE_PICKUP = "提货单\n合同编号：{cno}\n提货日期：2026-03-28\n仓库：上港库\n数量：500吨"
SALE_INVOICE = ("增值税专用发票\n发票号码：04213888\n开票日期：2026-03-25\n合同编号：{cno}\n"
                "销售方：华东供应链有限公司\n价税合计：¥350,000.00")
PURCHASE = ("采购合同\n合同编号：{cno}\n甲方：华东供应链有限公司\n乙方：华东电子有限公司\n"
            "签订日期：2026-05-02\n合同金额：¥452,000.00\n标的：电解铜 60 吨。")
PURCHASE_INSTOCK = "入库单\n合同编号：{cno}\n入库日期：2026-05-15\n仓库：松江库\n数量：60吨"


def _wait_job(client, job_id, timeout=40):
    for _ in range(timeout * 10):
        j = client.get(f"/api/processing/jobs/{job_id}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.1)
    raise AssertionError("任务超时")


def _ingest(client, tag: str, files: dict[str, str]) -> None:
    """每个用例独立目录，避免上一轮遗留在 inbox 的文件被重复处理。"""
    src = settings.inbox_root / tag
    src.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (src / name).write_text(body, encoding="utf-8")
    r = client.post("/api/processing/jobs", json={"source_dir": str(src), "recursive": True})
    assert r.status_code == 200, r.text
    _wait_job(client, r.json()["id"])


def _doc(db, name: str) -> Document:
    db.expire_all()
    d = db.query(Document).filter(Document.original_filename == name).first()
    assert d, name
    return d


def _ledger(db, specs: list[tuple], name: str = "真实台账"):
    p = settings.ledger_root / f"{name}.xlsx"
    p.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "合同台账"
    ws.append(["合同编号", "客户名称", "合同金额", "签订日期", "业务类别"])
    for row in specs:
        ws.append(list(row))
    wb.save(p)
    return svc.save_ledger(db, name, str(p), svc.read_ledger_file(p))


def test_attachment_keeps_its_own_type_and_role(client, db):
    """写着合同编号的附件不该被通用「合同」规则抢走类型；类型对了角色才对。"""
    cno = "XS-2026-6101"
    _ingest(client, "6101", {f"销售合同_{cno}.txt": SALE.format(cno=cno),
                             f"结算单_{cno}.txt": SALE_SETTLE.format(cno=cno),
                             f"提货单_{cno}.txt": SALE_PICKUP.format(cno=cno),
                             f"发票_{cno}.txt": SALE_INVOICE.format(cno=cno)})

    docs = [_doc(db, f"销售合同_{cno}.txt"), _doc(db, f"结算单_{cno}.txt"),
            _doc(db, f"提货单_{cno}.txt"), _doc(db, f"发票_{cno}.txt")]
    assert [d.document_type for d in docs] == ["合同", "结算单", "提货单", "发票"]
    for d in docs:
        # 带合同编号的单据是某笔业务的附件证据，规则命中就自动归档，不该堆进待审核
        assert d.status == "archived", (d.original_filename, d.status, d.confidence)
        assert not d.current_filename.endswith((".txt.txt", ".pdf.pdf")), d.current_filename
    assert {bundle_service.derive_role(db, d) for d in docs} == {
        "销售合同", "结算单", "提货单", "发票"}


def test_requirements_close_after_real_upload(client, db):
    """缺发票/缺提货单来自真实归档数据；补齐上传后包变齐备、差异不再出现。"""
    cno = "XS-2026-6102"
    _ingest(client, "6102", {f"销售合同_{cno}.txt": SALE.format(cno=cno),
                             f"结算单_{cno}.txt": SALE_SETTLE.format(cno=cno)})
    ledger = _ledger(db, [(cno, "北方钢铁贸易有限公司", 350000, "2026-03-05", "销售")],
                     name="真实台账6102")

    run = svc.run_reconciliation(db, ledger.id, "ledger")
    gaps = {d.detail["required_role"] for d in run.diffs if d.diff_type == "missing_required"}
    assert gaps == {"发票", "提货单"}, gaps
    assert "amount_mismatch" not in {d.diff_type for d in run.diffs}
    assert not [d for d in run.diffs if d.diff_type == "missing_doc"]

    _ingest(client, "6102b", {f"发票_{cno}.txt": SALE_INVOICE.format(cno=cno),
                              f"提货单_{cno}.txt": SALE_PICKUP.format(cno=cno)})
    db.expire_all()      # 后台 worker 提交过的行，本 session 要重新读才看得见
    run2 = svc.run_reconciliation(db, ledger.id, "ledger")
    assert not [d for d in run2.diffs if d.diff_type == "missing_required"]
    bid = svc.bundle_for_contract(db, cno).id
    detail = bundle_service.get_bundle_detail(db, bid)
    assert detail["complete"] is True, detail["completeness"]


def test_purchase_bundle_requires_instock(client, db):
    """采购合同的必备单据是入库单，不是提货单。"""
    cno = "CG-2026-6103"
    _ingest(client, "6103", {f"采购合同_{cno}.txt": PURCHASE.format(cno=cno),
                             f"入库单_{cno}.txt": PURCHASE_INSTOCK.format(cno=cno)})
    doc = _doc(db, f"入库单_{cno}.txt")
    assert doc.document_type == "入库单" and doc.status == "archived"

    bid = svc.bundle_for_contract(db, cno).id
    db.expire_all()
    rep = bundle_service.get_bundle_detail(db, bid)
    assert rep["groups"][0]["anchor_roles"] == ["采购合同"], rep["groups"][0]
    assert sorted(rep["completeness"]["missing"]) == ["发票", "结算单"]


def test_generic_contract_role_from_title(db):
    """分类只到父级「合同」时，靠标题证据分出销售/采购，必备清单才匹配得上。"""
    parent = db.query(Category).filter(Category.parent_id.is_(None),
                                       Category.name == "合同").first()
    assert parent

    def mk(name: str, text: str) -> Document:
        path = settings.document_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        doc = Document(original_filename=name, current_filename=name,
                       original_path=str(path), current_path=str(path), file_type="txt",
                       document_type="合同", suggested_category_id=parent.id,
                       archived_category_id=parent.id, status="archived",
                       processed_at=datetime.now(), archived_at=datetime.now())
        db.add(doc)
        db.flush()
        db.merge(DocumentText(document_id=doc.id, text=text, text_spaced=space_cjk(text)))
        db.commit()
        return doc

    by_name = mk("采购合同6104.txt", "甲乙双方签署如下条款")
    assert bundle_service.derive_role(db, by_name) == "采购合同"
    by_header = mk("扫描件0001.txt", SALE.format(cno="XS-2026-6104"))
    assert bundle_service.derive_role(db, by_header) == "销售合同"
    ambiguous = mk("合同文本.txt", "甲乙双方签署如下条款")
    assert bundle_service.derive_role(db, ambiguous) == "合同"


def test_invoice_and_settlement_amounts_are_not_double_counted(db):
    """发票合计、结算单合计各自都等于合同金额；加在一起比会全员假报警。"""
    from tests.test_bundles import _invoice, _sale, _settle

    ok = "XS-2026-6105"
    bad = "XS-2026-6106"
    ledger = _ledger(db, [(ok, "北方钢铁贸易有限公司", 350000, "2026-03-05", "销售"),
                          (bad, "北方钢铁贸易有限公司", 350000, "2026-03-05", "销售")],
                     name="分桶台账")
    _sale(db, "6105", ok)
    _invoice(db, "6105", ok)
    _settle(db, "6105", ok)
    _sale(db, "6106", bad)
    _invoice(db, "6106", bad, amount=300000.0)
    _settle(db, "6106", bad)

    run = svc.run_reconciliation(db, ledger.id, "ledger")
    mism = [d for d in run.diffs if d.diff_type == "amount_mismatch"]
    assert [d.contract_no for d in mism] == [bad], [(d.contract_no, d.actual) for d in mism]
    assert mism[0].detail["doc_type"] == "发票"
    assert mism[0].actual == "300000.00" and mism[0].expected == "350000.00"


def test_batch_counters_do_not_lose_increments(db):
    """进度计数交给 SQL 原子累加：多条线程同时 +1 也不能丢，否则进度条到不了 100%。"""
    import threading

    from backend.models import ProcessingJob
    from backend.services.pipeline import pipeline

    job = ProcessingJob(source="test", kind="batch", total=24, status="running")
    db.add(job)
    db.commit()
    job_id = job.id

    def work():
        for _ in range(6):
            pipeline._bump(None, job_id, "archived")

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    db.rollback()
    db.expire_all()
    j = db.get(ProcessingJob, job_id)
    assert j.archived == 24, f"丢了 {24 - j.archived} 次增量"
    assert j.processed == 24
