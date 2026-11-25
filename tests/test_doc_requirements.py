"""必备单据清单可改；老库升级要能补上新类型、新索引和新列。"""
from __future__ import annotations

import openpyxl

from backend.config import settings
from backend.models import Category, CategoryKeyword, ClassifyRule, DocRequirement
from backend.services import reconciliation as svc
from backend.services.defaults import seed_missing
from tests.test_bundles import _invoice, _pickup, _sale, _settle


def _ledger(db, specs, name: str = "规则台账"):
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


def _roles(db, cno: str) -> set[str]:
    run = svc.run_reconciliation(db, None, "ledger")
    return {d.detail["required_role"] for d in run.diffs
            if d.diff_type == "missing_required" and d.contract_no == cno}


def test_seeded_requirements(db):
    rows = {(r.contract_role, r.required_role) for r in db.query(DocRequirement).all()}
    assert ("销售合同", "发票") in rows
    assert ("销售合同", "结算单") in rows
    assert ("销售合同", "提货单") in rows
    assert ("采购合同", "入库单") in rows
    assert ("销售合同", "银行回单") not in rows   # 回款走 missing_payment，别重复报


def test_disabling_requirement_stops_the_diff(db):
    """关掉「提货单」后不再报该缺失；重新打开又会出现。"""
    cno = "XS-2026-9001"
    _ledger(db, [(cno, "北方钢铁贸易有限公司", 350000, "2026-03-05", "销售")])
    _sale(db, "9001", cno)
    _settle(db, "9001", cno)
    _pickup(db, "9001", cno)
    assert "提货单" not in _roles(db, cno)

    cno2 = "XS-2026-9002"
    _ledger(db, [(cno2, "北方钢铁贸易有限公司", 350000, "2026-03-05", "销售")], name="规则台账2")
    _sale(db, "9002b", cno2)
    _settle(db, "9002b", cno2)
    assert "提货单" in _roles(db, cno2)

    row = db.query(DocRequirement).filter(DocRequirement.contract_role == "销售合同",
                                          DocRequirement.required_role == "提货单").first()
    row.enabled = False
    db.commit()
    db.query(svc.ReconRun).delete()
    db.query(svc.ReconDiff).delete()
    db.commit()
    assert "提货单" not in _roles(db, cno2)
    assert "发票" in _roles(db, cno2)


def test_min_count_and_amount_check_editable(db, client):
    """min_count=2 时要两份才算齐；amount_check 只是标记，不额外报差异。"""
    r = client.put("/api/bundles/requirements", json=[
        {"contract_role": "销售合同", "required_role": "发票", "min_count": 2,
         "amount_check": False, "enabled": True},
    ])
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1 and rows[0]["min_count"] == 2
    assert rows[0]["amount_check"] is False
    assert db.query(DocRequirement).count() == 1   # 整表替换：不在载荷里的会被删掉

    cno = "XS-2026-9003"
    _ledger(db, [(cno, "北方钢铁贸易有限公司", 350000, "2026-03-05", "销售")], name="数量台账")
    _sale(db, "9003", cno)
    _invoice(db, "9003", cno)
    assert "发票" in _roles(db, cno)              # 只有一份，仍算缺

    client.put("/api/bundles/requirements", json=[
        {"contract_role": "销售合同", "required_role": "发票", "min_count": 1,
         "amount_check": True, "enabled": True}])
    db.query(svc.ReconRun).delete()
    db.query(svc.ReconDiff).delete()
    db.commit()
    assert "发票" not in _roles(db, cno)


def test_seed_missing_backfills_old_database(db):
    """老库（没有提货单分类/规则/清单）升级后必须补齐，且重复调用不产生重复项。"""
    db.query(ClassifyRule).filter(ClassifyRule.name == "提货单").delete()
    db.query(Category).filter(Category.name == "提货单").delete()
    db.query(DocRequirement).filter(DocRequirement.required_role == "提货单").delete()
    db.commit()
    assert db.query(Category).filter(Category.name == "提货单").count() == 0

    seed_missing(db)
    db.expire_all()
    cat = db.query(Category).filter(Category.name == "提货单").first()
    assert cat and cat.path == "业务/提货单" and cat.parent_id is not None
    assert db.query(ClassifyRule).filter(ClassifyRule.name == "提货单").count() == 1
    assert db.query(DocRequirement).filter(
        DocRequirement.contract_role == "销售合同",
        DocRequirement.required_role == "提货单").count() == 1

    before = (db.query(Category).count(), db.query(ClassifyRule).count(),
              db.query(DocRequirement).count(), db.query(CategoryKeyword).count())
    seed_missing(db)
    seed_missing(db)
    db.expire_all()
    after = (db.query(Category).count(), db.query(ClassifyRule).count(),
             db.query(DocRequirement).count(), db.query(CategoryKeyword).count())
    assert before == after

    # 用户自己改过的值不会被种子覆盖回去
    row = db.query(DocRequirement).filter(DocRequirement.contract_role == "销售合同",
                                          DocRequirement.required_role == "发票").first()
    row.min_count = 3
    db.commit()
    seed_missing(db)
    db.expire_all()
    assert db.get(DocRequirement, row.id).min_count == 3


def test_legacy_db_gets_the_source_column(db, client):
    """v2.2 的库没有 recon_runs.source：补列一旦失败，台账对账页整页 500。"""
    from sqlalchemy import text

    from backend.database import init_db

    db.execute(text("ALTER TABLE recon_runs DROP COLUMN source"))
    db.commit()
    cols = {r[1] for r in db.execute(text("PRAGMA table_info(recon_runs)"))}
    assert "source" not in cols, "用例没把老库模拟出来"

    init_db()
    db.expire_all()
    cols = {r[1] for r in db.execute(text("PRAGMA table_info(recon_runs)"))}
    assert "source" in cols, "补列又失败了（raw 连接只接受字符串 SQL）"
    assert client.get("/api/reconciliation/runs").status_code == 200
