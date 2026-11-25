"""有据对账：每条差异都能反查依据、补单据、就地重算并自动关闭。"""
from __future__ import annotations

import time

import openpyxl

from backend.config import settings
from backend.models import ReconDiff, ReconRun
from backend.services import reconciliation as svc
from tests.test_bundles import _cat, _invoice, _pickup, _sale, _settle


def _ledger(db, specs: list[tuple], name: str = "有据台账"):
    p = settings.ledger_root / f"{name}.xlsx"
    p.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "合同台账"
    ws.append(["合同编号", "客户名称", "合同金额", "签订日期", "业务类别", "我方主体"])
    for row in specs:
        ws.append(list(row))
    wb.save(p)
    return svc.save_ledger(db, name, str(p), svc.read_ledger_file(p))


def _types(run) -> set[str]:
    return {d.diff_type for d in run.diffs}


def test_anchored_contract_reports_missing_required_not_missing_doc(db):
    """有合同锚点 → 按必备清单报缺发票/缺提货单，不再重复报粗粒度 missing_doc。"""
    cno = "XS-2026-8001"
    ledger = _ledger(db, [(cno, "北方钢铁贸易有限公司", 350000, "2026-03-05", "销售",
                          "华东供应链有限公司")])
    _sale(db, "8001", cno)
    _settle(db, "8001", cno)

    run = svc.run_reconciliation(db, ledger.id, "ledger")
    assert run.source == "ledger"
    assert "missing_doc" not in _types(run)
    req = [d for d in run.diffs if d.diff_type == "missing_required"]
    assert {d.detail["required_role"] for d in req} == {"发票", "提货单"}
    assert all(d.detail["bundle_id"] for d in req)
    assert {d.severity for d in req} == {"error", "warn"}
    # 回款另算，不进必备清单，避免同一件事报两条
    assert "missing_payment" in _types(run)
    assert not any(d.detail.get("required_role") in ("回款凭证", "银行回单") for d in req)


def test_unanchored_contract_reports_missing_doc_only(db):
    """档案库连合同都没有 → 只报 missing_doc，不能要求一份不存在的合同的附件。"""
    cno = "CG-2026-8002"
    ledger = _ledger(db, [(cno, "华东电子有限公司", 88000, "2026-04-01", "采购",
                           "华东供应链有限公司")])
    run = svc.run_reconciliation(db, ledger.id, "ledger")
    assert "missing_doc" in _types(run)
    assert not [d for d in run.diffs if d.diff_type == "missing_required"]
    assert run.diffs[0].detail["bundle_id"] is None


def test_amount_mismatch_detail_carries_document_ids(db):
    """detail.docs 必须是文档 id，抽屉才能直接出缩略图。"""
    cno = "XS-2026-8003"
    ledger = _ledger(db, [(cno, "北方钢铁贸易有限公司", 350000, "2026-03-05", "销售",
                           "华东供应链有限公司")])
    _sale(db, "8003", cno)
    inv = _invoice(db, "8003", cno, amount=300000.0)
    _pickup(db, "8003", cno)

    run = svc.run_reconciliation(db, ledger.id, "ledger")
    bad = [d for d in run.diffs if d.diff_type == "amount_mismatch"]
    assert len(bad) == 1
    assert bad[0].detail["docs"] == [inv.id]
    assert isinstance(bad[0].detail["docs"][0], int)
    assert bad[0].expected == "350000.00" and bad[0].actual == "300000.00"


def test_evidence_endpoint_shape(db, client):
    cno = "XS-2026-8004"
    ledger = _ledger(db, [(cno, "北方钢铁贸易有限公司", 350000, "2026-03-05", "销售",
                           "华东供应链有限公司")])
    sale = _sale(db, "8004", cno)
    st = _settle(db, "8004", cno)
    run = svc.run_reconciliation(db, ledger.id, "ledger")
    miss = [d for d in run.diffs if d.diff_type == "missing_required"
            and d.detail["required_role"] == "发票"][0]

    r = client.get(f"/api/reconciliation/diffs/{miss.id}/evidence")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bundle"]["id"] == miss.detail["bundle_id"]
    assert {d["document_id"] for d in body["docs"]} == {sale.id, st.id}
    assert {d["is_anchor"] for d in body["docs"]} == {True, False}
    assert body["group"]["contract_no"] == cno
    assert [q["role"] for q in body["missing"]] == ["发票", "提货单"]
    assert body["diff"]["role"] == "发票"
    assert body["run"]["source"] == "ledger"
    assert client.get("/api/reconciliation/diffs/999999/evidence").status_code == 404


def _wait_job(client, job_id, timeout=25):
    for _ in range(timeout * 10):
        j = client.get(f"/api/processing/jobs/{job_id}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.1)
    raise AssertionError("任务超时")


def test_upload_from_diff_then_recheck_auto_closes(db, client):
    """差异处上传发票 → 自动识别归档入包 → 复核后差异就地关闭并留痕文档 id。"""
    from backend.models import Document

    cno = "XS-2026-8005"
    ledger = _ledger(db, [(cno, "北方钢铁贸易有限公司", 350000, "2026-03-05", "销售",
                           "华东供应链有限公司")])
    _sale(db, "8005", cno)
    _settle(db, "8005", cno)
    _pickup(db, "8005", cno)
    run = svc.run_reconciliation(db, ledger.id, "ledger")
    gap = [d for d in run.diffs if d.diff_type == "missing_required"
           and d.detail["required_role"] == "发票"][0]
    bid = gap.detail["bundle_id"]

    body = "\n".join([
        "上海增值税专用发票",
        "发票号码：04213888",
        f"合同编号：{cno}",
        "价税合计：¥350,000.00",
        "销售方：华东供应链有限公司",
        "购买方：北方钢铁贸易有限公司",
    ])
    r = client.post(f"/api/reconciliation/diffs/{gap.id}/upload-doc",
                    files={"files": ("增值税专用发票8005.txt", body.encode("utf-8"),
                                      "text/plain")})
    assert r.status_code == 200, r.text
    up = r.json()
    assert up["job_id"] and len(up["saved"]) == 1
    job = _wait_job(client, up["job_id"])
    assert job["processed"] == 1, job

    db.expire_all()
    doc = db.query(Document).filter(Document.original_filename.like("%发票8005%")).first()
    assert doc, "上传的发票没进档案库"
    if doc.status != "archived":
        r = client.post(f"/api/review/{doc.id}/approve",
                        json={"category_id": _cat(db, "发票").id, "document_type": "发票"})
        assert r.status_code == 200, r.text

    r = client.post(f"/api/reconciliation/diffs/{gap.id}/recheck")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["resolved"] is True and out["changed"] is True
    assert out["closed_by"] == [doc.id]
    assert str(doc.id) in out["note"] and "自动补齐" in out["note"]
    assert f"#{doc.id}" in out["note"]
    assert out["evidence"]["bundle_id"] == bid
    db.expire_all()
    fresh = db.get(ReconDiff, gap.id)
    assert fresh.resolved and fresh.detail["closed_by"] == [doc.id]
    # 入包后可齐备度立刻反映
    rep = out["evidence"]
    assert rep["bundle"]["id"] == bid
    assert rep["bundle"]["complete"] is True
    assert [q["role"] for q in rep["missing"]] == []

    # 重复复核不再变化
    again = client.post(f"/api/reconciliation/diffs/{gap.id}/recheck").json()
    assert again["resolved"] is True and again["changed"] is False


def test_recheck_reports_still_missing(db, client):
    """补错东西不能算过关：仍缺的角色要原地显示。"""
    cno = "XS-2026-8006"
    ledger = _ledger(db, [(cno, "北方钢铁贸易有限公司", 350000, "2026-03-05", "销售",
                           "华东供应链有限公司")])
    _sale(db, "8006", cno)
    run = svc.run_reconciliation(db, ledger.id, "ledger")
    gap = [d for d in run.diffs if d.diff_type == "missing_required"
           and d.detail["required_role"] == "发票"][0]

    out = client.post(f"/api/reconciliation/diffs/{gap.id}/recheck").json()
    assert out["resolved"] is False and out["remaining"] == ["发票"]
    assert out["evidence"]["still_missing"] == ["发票"]


def test_resolve_and_recheck_recount_counts(db, client):
    cno = "XS-2026-8007"
    ledger = _ledger(db, [(cno, "北方钢铁贸易有限公司", 350000, "2026-03-05", "销售",
                           "华东供应链有限公司")])
    _sale(db, "8007", cno)
    run = svc.run_reconciliation(db, ledger.id, "ledger")
    before = dict(run.counts)
    assert sum(before.values()) == len(run.diffs)

    gap = [d for d in run.diffs if d.diff_type == "missing_required"][0]
    r = client.post(f"/api/reconciliation/diffs/{gap.id}/resolve", json={"note": "线下已补"})
    assert r.status_code == 200
    db.expire_all()
    after = db.get(ReconRun, run.id).counts
    assert sum(after.values()) == sum(before.values()) - 1
    assert after[gap.diff_type] == before[gap.diff_type] - 1


def test_archive_source_self_reconciliation(db):
    """没有台账也能对账：以已归档合同为锚，检查项与台账模式完全一致。"""
    cno = "XS-2026-8008"
    _sale(db, "8008", cno)
    _settle(db, "8008", cno)

    run = svc.run_reconciliation(db, None, "auto")   # 无台账 → 自动降级为档案库自对账
    assert run.source == "archive"
    assert run.ledger_id is None
    assert run.total_rows == 1, [d.diff_type for d in run.diffs]
    req = {d.detail["required_role"] for d in run.diffs if d.diff_type == "missing_required"}
    assert req == {"发票", "提货单"}
    assert "missing_payment" in _types(run)
    assert "missing_doc" not in _types(run)
    assert "amount_mismatch" not in _types(run)   # 结算单金额与合同一致
    assert all(d.contract_no == cno for d in run.diffs)
    assert run.counts == {"missing_required": 2, "missing_payment": 1}


def test_archive_source_picks_up_amount_and_seal(db):
    """自对账同样能抓金额不符与缺章。"""
    from backend.models import Document

    cno = "XS-2026-8009"
    sale = _sale(db, "8009", cno, amount=200000.0)
    _invoice(db, "8009", cno, amount=150000.0)
    _pickup(db, "8009", cno)
    d = db.get(Document, sale.id)
    d.has_seal = False
    db.commit()

    run = svc.run_reconciliation(db, None, "archive")
    assert run.source == "archive"
    types = _types(run)
    assert "amount_mismatch" in types and "seal_missing" in types
    assert {x.detail["required_role"] for x in run.diffs
            if x.diff_type == "missing_required"} == {"结算单"}


def test_ledger_source_without_ledger_is_a_clear_error(db, client):
    r = client.post("/api/reconciliation/runs", json={"source": "ledger"})
    assert r.status_code == 400
    assert "台账" in r.json()["detail"]


def test_recon_run_api_reports_source(db, client):
    cno = "XS-2026-8010"
    _sale(db, "8010", cno)
    r = client.post("/api/reconciliation/runs", json={"source": "auto"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "archive"
    runs = client.get("/api/reconciliation/runs").json()
    assert runs[0]["source"] == "archive"
    diffs = client.get(f"/api/reconciliation/runs/{body['run_id']}/diffs").json()
    assert any(d["type"] == "missing_required" for d in diffs)
    assert all(d["label"] == "缺必备单据" for d in diffs if d["type"] == "missing_required")


def test_amount_recheck_uses_the_recorded_bucket(db, client):
    """复核与出差异同一口径：只比当初报警那一类单据，补齐它就关闭。"""
    cno = "XS-2026-8011"
    ledger = _ledger(db, [(cno, "北方钢铁贸易有限公司", 350000, "2026-03-05", "销售",
                          "华东供应链有限公司")])
    _sale(db, "8011", cno)
    inv = _invoice(db, "8011", cno, amount=300000.0)
    _settle(db, "8011", cno)        # 结算单本身是对的，不该被算进发票的账
    _pickup(db, "8011", cno)

    run = svc.run_reconciliation(db, ledger.id, "ledger")
    bad = [d for d in run.diffs if d.diff_type == "amount_mismatch"]
    assert len(bad) == 1, [(d.actual, d.detail) for d in bad]
    assert bad[0].detail["doc_type"] == "发票"
    assert bad[0].detail["docs"] == [inv.id]
    assert bad[0].actual == "300000.00"

    out = client.post(f"/api/reconciliation/diffs/{bad[0].id}/recheck").json()
    assert out["resolved"] is False and out["remaining"] == ["金额"], out

    extra = _invoice(db, "8011b", cno, amount=50000.0)   # 补开 5 万，发票合计 35 万
    out2 = client.post(f"/api/reconciliation/diffs/{bad[0].id}/recheck").json()
    assert out2["resolved"] is True, out2
    assert out2["closed_by"] == [extra.id]
    db.expire_all()
    assert db.get(ReconDiff, bad[0].id).resolved


def test_evidence_bundle_survives_a_rebuild(db, client):
    """重建归集会删掉自动包再建，主键被 SQLite 复用：差异的依据不能串到别的业务上。"""
    from backend.models import Bundle

    cno_a, cno_b = "XS-2026-8012", "XS-2026-8013"
    ledger = _ledger(db, [
        (cno_a, "北方钢铁贸易有限公司", 350000, "2026-03-05", "销售", "华东供应链有限公司"),
        (cno_b, "华东电子有限公司", 120000, "2026-03-08", "销售", "华东供应链有限公司"),
    ])
    _sale(db, "8012", cno_a)
    _sale(db, "8013", cno_b, amount=120000.0, date="2026-03-08", party="华东电子有限公司")
    _settle(db, "8013", cno_b, amount=120000.0)

    run = svc.run_reconciliation(db, ledger.id, "ledger")
    diff = [d for d in run.diffs if d.contract_no == cno_a
            and d.diff_type == "missing_required"][0]
    assert client.post("/api/bundles/rebuild").status_code == 200

    db.expire_all()
    wrong = [b for b in db.query(Bundle).all() if b.contract_key != svc.norm_no(cno_a)][0]
    # 重建之后旧 id 撞上别的包，等价于把 bundle_id 指到一个明确错误的业务上
    diff.detail = {**(diff.detail or {}), "bundle_id": wrong.id}
    db.commit()

    r = client.get(f"/api/reconciliation/diffs/{diff.id}/evidence")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bundle_id"] != wrong.id
    assert body["bundle"]["contract_key"] == svc.norm_no(cno_a)
    assert body["group"]["contract_no"] == cno_a
    assert [q["role"] for q in body["missing"]] == ["发票", "结算单", "提货单"]
