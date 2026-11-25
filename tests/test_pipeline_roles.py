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


# 真实业务文档：标题写着「矿石销售合同」，正文里到处是发票/结算/提货字样（合同条款必然要写）
ORE_SALE = ("矿石销售合同\n签约地点：山西省临汾市尧都区\n乙方：山西宏达钢铁有限公司（买方）\n"
            "签约时间：2026年5月12日\n合同编号：HSYCXS（DD）-2026-0236\n"
            "一、标的物：PB粉 10000吨，合同车板现汇含税价 799.05 元/吨。\n"
            "二、结算方式：货到验收后30天内电汇，乙方凭甲方开具的增值税发票付款。\n"
            "三、提货方式：乙方自提，提货费用由乙方承担。\n")
ORE_SETTLE = ("华山云创供应链科技（山西）有限公司销售结算单\nTo:\n山西宏达钢铁有限公司\nDate:\n"
              "2026/6/25\n贵我双方合同HSYCXS（DD）-2026-0236下，10000吨PB粉提货完毕，结算如下：\n"
              "最终结算单价 799.09 元/吨，请据此开具发票\n")


def test_body_mentions_do_not_steal_the_type(client, db):
    """正文提到「发票」不等于这份文件是发票——合同条款必然写着开票与提货。

    真实翻车样本：一份矿石销售合同被识别成发票（发票规则 0.92 > 合同 0.90），
    整笔业务因此没有合同锚，归集与必备单据检查全断。
    """
    _ingest(client, "6110", {"a7f3c91b2d.txt": ORE_SALE, "b8e4d02c3e.txt": ORE_SETTLE})

    sale = _doc(db, "a7f3c91b2d.txt")
    settle = _doc(db, "b8e4d02c3e.txt")
    assert sale.document_type == "合同", (sale.document_type, sale.confidence_detail)
    assert settle.document_type == "结算单", (settle.document_type, settle.confidence_detail)
    # 分类要落到合同叶子：归档目录和业务角色都从这里来

    assert db.get(Category, sale.archived_category_id).path == "合同/销售合同"
    assert db.get(Category, settle.archived_category_id).path == "合同/结算单"
    assert bundle_service.derive_role(db, sale) == "销售合同"
    assert bundle_service.derive_role(db, settle) == "结算单"

    from backend.models import DocumentField

    fmap = {f.name: f.value for f in db.query(DocumentField).filter_by(document_id=sale.id).all()}
    # 合同没有明示总额时按单价×数量补，绝不能把 799.05 当成交金额（对账会报假差异）
    assert fmap["金额"] == "7990500.00", fmap
    assert fmap["单价"] == "799.05", fmap

    bid = svc.bundle_for_contract(db, "HSYCXS（DD）-2026-0236").id
    detail = bundle_service.get_bundle_detail(db, bid)
    roles = {a["role"] for g in detail["groups"] for a in g["anchors"]}
    atts = {a["role"] for g in detail["groups"] for a in g["attachments"]}
    assert roles == {"销售合同"}, roles
    assert atts == {"结算单"}, atts
    assert sorted(detail["groups"][0]["missing"]) == ["发票", "提货单"], detail["completeness"]


def test_contract_no_from_filename_only(client, db):
    """扫描件正文里常常一个编号都没有，编号只在文件名：HSYCXS（DD）-2026-0236。

    短号正则不认全角括号时只能拿到 HSYCXS，同系列的合同会被塞进同一个业务包。
    """
    from backend.models import DocumentField

    cno = "HSYCXS（DD）-2026-0248"
    body = ORE_SALE.replace("合同编号：HSYCXS（DD）-2026-0236", "签约主体：华北矿业有限公司")
    _ingest(client, "6112", {f"{cno}.txt": body})

    doc = _doc(db, f"{cno}.txt")
    assert doc.document_type == "合同"
    got = db.query(DocumentField).filter(DocumentField.document_id == doc.id,
                                         DocumentField.name == "合同编号").first()
    assert got and got.value == cno, got.value if got else None
    assert svc.bundle_for_contract(db, cno) is not None
    # 文件名就是合同号时别再叠一遍，也不要留 .pdf-1 这类上传残留
    assert doc.current_filename.count("HSYCXS") == 1, doc.current_filename
    assert not doc.current_filename.endswith(".txt.txt"), doc.current_filename


def test_reprocess_reruns_in_place(client, db):
    """重新识别必须原地重跑。

    以前它会把这条记录自己的 sha256 当成「与另一条重复」，结果旧记录卡在 pending、
    凭空多一条 duplicate，用户点完「重新识别」看到的仍是错的类型。
    """
    cno = "XS-2026-6113"
    _ingest(client, "6113", {f"销售合同_{cno}.txt": SALE.format(cno=cno)})
    doc = _doc(db, f"销售合同_{cno}.txt")
    doc_id = doc.id
    before = db.query(Document).count()

    r = client.post(f"/api/documents/{doc_id}/reprocess")
    assert r.status_code == 200, r.text
    _wait_job(client, r.json()["job_id"])

    db.expire_all()
    assert db.query(Document).count() == before, "重新识别不该新增记录"
    assert not db.query(Document).filter(Document.status == "duplicate").all()
    fresh = db.get(Document, doc_id)
    assert fresh.status == "archived", (fresh.status, fresh.error)
    assert fresh.document_type == "合同"
    assert fresh.archived_category_id == doc.archived_category_id
    # 命名用的字段必须是这一轮刚写进去的，不能是关系缓存里的旧集合
    assert fresh.current_filename.count(cno) == 1, fresh.current_filename
    assert not fresh.current_filename.endswith((".txt.txt", ".pdf.pdf")), fresh.current_filename


def test_saved_sample_beats_generic_rule(client, db):
    """用户确认过的同模板样例必须压过通用规则，否则改了也白改。

    这条正文抬手里没有结算单字样，规则只会说它是合同；样例说了算。
    """
    from backend.models import Category, DocumentSample
    from backend.services.fingerprint import fingerprint as make_fp

    cat = db.query(Category).filter(Category.path == "合同/结算单").first()
    text = ("华山云创供应链科技（山西）有限公司\nTo:\n山西宏达钢铁有限公司\nDate:\n"
            "贵我双方合同HSYCXS（DD）-2026-0247下10000吨PB粉完毕，明细如下：\n"
            "最终单价 799.09 元/吨，请据此开具发票\n")
    db.add(DocumentSample(name="已确认模板", path="sample://6111", document_type="结算单",
                          category_id=cat.id, text_norm=make_fp(text)))
    db.commit()
    _ingest(client, "6111", {"c9f5e13d4f.txt": text})

    doc = _doc(db, "c9f5e13d4f.txt")
    assert doc.document_type == "结算单", (doc.document_type, doc.confidence_detail)


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

def test_reprocess_clears_stale_rule_fields(client, db):
    """重新识别要清掉上一轮规则留下的字段，但不能动人工修正过的值。

    只覆盖同名项的话，误判当年写进去的假「公司名称」会一直挂着，归集与对账都吃它。
    """
    from backend.models import DocumentField

    cno = "XS-2026-6114"
    _ingest(client, "6114", {f"销售合同_{cno}.txt": SALE.format(cno=cno)})
    doc = _doc(db, f"销售合同_{cno}.txt")
    # 陈旧字段：这一轮抽取不出「项目名称」；人工字段：金额是用户自己填的
    db.add(DocumentField(document_id=doc.id, name="项目名称", value="老项目",
                         source="rule", confidence=0.7))
    db.query(DocumentField).filter_by(document_id=doc.id, name="金额").update(
        {"value": "123456.00", "source": "manual", "confidence": 1.0})
    db.commit()

    r = client.post(f"/api/documents/{doc.id}/reprocess")
    assert r.status_code == 200, r.text
    _wait_job(client, r.json()["job_id"])
    db.expire_all()
    got = {f.name: (f.value, f.source)
           for f in db.query(DocumentField).filter_by(document_id=doc.id).all()}
    assert "项目名称" not in got, got
    assert got["金额"] == ("123456.00", "manual"), got
    assert got["合同编号"] == (cno, "rule"), got
