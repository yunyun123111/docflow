"""对账：台账解析 → 匹配 → 差异分类 → 结转/导出。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import openpyxl

from backend.config import settings
from backend.models import Category, Document, DocumentField, Ledger, STATUS_ARCHIVED


def _mk_ledger() -> Path:
    p = settings.ledger_root / "台账.xlsx"
    p.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "合同台账"
    ws.append(["合同编号", "客户名称", "合同金额", "签订日期", "业务类别"])
    ws.append(["XS-2026-0088", "北方钢铁贸易有限公司", 350000, "2026-03-05", "销售"])
    ws.append(["CG-2026-0011", "华东电子有限公司", 88000, "2026-04-01", "采购"])
    s = wb.create_sheet("结算明细")
    s.append(["合同编号", "客户", "结算金额", "日期"])
    s.append(["XS-2026-0088", "北方钢铁", 350000, "2026-03-20"])
    pay = wb.create_sheet("回款记录")
    pay.append(["合同编号", "金额", "到账日期"])
    pay.append(["XS-2026-0088", 350000, "2026-04-02"])
    wb.save(p)
    return p


def _archive_doc(db, contract_no: str, customer: str, amount: float, doc_type: str,
                 name: str, date: str = "2026-03-20") -> Document:
    doc = Document(original_filename=name, current_filename=name,
                   original_path=str(settings.document_root / name),
                   current_path=str(settings.document_root / name),
                   file_type="txt", document_type=doc_type, status=STATUS_ARCHIVED,
                   processed_at=datetime.now(), archived_at=datetime.now())
    db.add(doc)
    db.flush()
    for k, v in (("合同编号", contract_no), ("对方单位", customer),
                 ("金额", f"{amount:.2f}"), ("日期", date)):
        db.add(DocumentField(document_id=doc.id, name=k, value=v, source="rule", confidence=0.9))
    db.commit()
    return doc


def test_ledger_parse_and_run(client, db):
    from backend.services import reconciliation as svc

    p = _mk_ledger()
    parsed = svc.read_ledger_file(p)
    assert parsed["sheets"]["合同台账"] == "contract"
    assert parsed["sheets"]["结算明细"] == "settlement"
    assert len(parsed["contract"]) == 2
    assert len(parsed["settlement"]) == 1 and parsed["payment"][0]["amount"] == 350000

    ledger = svc.save_ledger(db, "台账A", str(p), parsed)
    assert len(ledger.rows) == 2

    _archive_doc(db, "XS-2026-0088", "北方钢铁贸易有限公司", 350000, "结算单", "结算单088.txt")
    _archive_doc(db, "XS-2026-0088", "北方钢铁贸易有限公司", 350000, "银行回单", "回单088.txt")

    run = svc.run_reconciliation(db, ledger.id)
    types = {d.diff_type for d in run.diffs}
    # 合同 2 无单据 → missing_doc；合同1 完整无 missing
    assert "missing_doc" in types
    open_011 = [d for d in run.diffs if d.contract_no.startswith("CG") and d.diff_type == "missing_doc"]
    assert open_011
    none_088 = [d for d in run.diffs if d.contract_no.startswith("XS") and d.diff_type == "missing_doc"]
    assert not none_088

    # 处理 CG-2026-0011 的差异 → 再跑一期 → 标记结转
    svc.resolve_diff(db, open_011[0], note="补录中")
    run2 = svc.run_reconciliation(db, ledger.id)
    carried = [d for d in run2.diffs if d.key == open_011[0].key]
    assert carried and carried[0].resolved and carried[0].resolution_note == "补录中"

    # 金额不符检测
    _archive_doc(db, "XS-2026-0088", "北方钢铁贸易有限公司", 300000, "结算单", "结算单088多.txt")
    run3 = svc.run_reconciliation(db, ledger.id)
    mismatch = [d for d in run3.diffs if d.diff_type == "amount_mismatch" and d.contract_no.startswith("XS")]
    assert mismatch, [d.diff_type for d in run3.diffs]

    # 导出
    r = client.get(f"/api/reconciliation/runs/{run3.id}/export?fmt=csv")
    assert r.status_code == 200 and "合同编号" in r.content.decode("utf-8-sig")
    r = client.get(f"/api/reconciliation/runs/{run3.id}/export?fmt=xlsx")
    assert r.status_code == 200 and r.content[:2] == b"PK"


def test_ledger_upload_api(client, db):
    p = _mk_ledger()
    r = client.post("/api/reconciliation/ledgers",
                    data={"name": "上传台账"},
                    files={"file": ("台账.xlsx", p.read_bytes(),
                                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows"] == 2
    runs = client.post("/api/reconciliation/runs", json={"ledger_id": body["id"]})
    assert runs.status_code == 200
    diffs = client.get(f"/api/reconciliation/runs/{runs.json()['run_id']}/diffs").json()
    assert isinstance(diffs, list)


def test_delete_run_removes_diffs(client, db):
    """对账期可删：删完期和差异都清除，不能留下测试数据。"""
    from backend.models import ReconDiff, ReconRun

    p = _mk_ledger()
    r = client.post("/api/reconciliation/ledgers", data={"name": "待删账台"},
                    files={"file": ("台账.xlsx", p.read_bytes(),
                                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    ledger_id = r.json()["id"]
    run_id = client.post("/api/reconciliation/runs", json={"ledger_id": ledger_id}).json()["run_id"]
    assert client.get(f"/api/reconciliation/runs/{run_id}/diffs").json()

    assert client.delete(f"/api/reconciliation/runs/{run_id}").status_code == 200
    assert db.get(ReconRun, run_id) is None
    assert db.query(ReconDiff).filter(ReconDiff.run_id == run_id).count() == 0
    assert client.delete(f"/api/reconciliation/runs/{run_id}").status_code == 404
