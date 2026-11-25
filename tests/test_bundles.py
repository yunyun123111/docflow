"""文档归集：自动建包、角色分组、手工维护、合并建议、重建回填。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from backend.config import settings
from backend.models import (
    Bundle, BundleItem, Category, Document, DocumentField, STATUS_ARCHIVED,
)
from backend.services import bundles as svc


def _cat(db, name: str) -> Category:
    c = db.query(Category).filter(Category.name == name).first()
    assert c, f"种子分类缺失: {name}"
    return c


def _doc(db, filename: str, doc_type: str, cat_name: str | None, *, cno: str = "",
         amount: float | None = None, date: str = "", party: str = "",
         company: str = "") -> Document:
    """造一份“已归档”的文档：分类决定角色，字段决定取数。"""
    p = settings.document_root / filename
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{doc_type} {filename}", encoding="utf-8")
    doc = Document(original_filename=filename, current_filename=filename,
                   original_path=str(p), current_path=str(p), file_type="txt",
                   document_type=doc_type, status=STATUS_ARCHIVED,
                   archived_category_id=_cat(db, cat_name).id if cat_name else None,
                   processed_at=datetime.now(), archived_at=datetime.now())
    db.add(doc)
    db.flush()
    values = {"合同编号": cno, "对方单位": party, "公司名称": company,
              "日期": date, "金额": f"{amount:.2f}" if amount is not None else ""}
    for k, v in values.items():
        if v:
            db.add(DocumentField(document_id=doc.id, name=k, value=v, source="rule",
                                 confidence=0.9))
    db.commit()
    svc.attach_document(db, doc)
    return doc


def _sale(db, n, cno, amount=350000.0, date="2026-03-05", party="北方钢铁贸易有限公司"):
    return _doc(db, f"销售合同{n}.txt", "合同", "销售合同", cno=cno, amount=amount,
                date=date, party=party, company="华东供应链有限公司")


def _settle(db, n, cno, amount=350000.0, date="2026-03-20"):
    return _doc(db, f"结算单{n}.txt", "结算单", "结算单", cno=cno, amount=amount, date=date)


def _invoice(db, n, cno, amount=350000.0, date="2026-03-25"):
    return _doc(db, f"发票{n}.txt", "发票", "发票", cno=cno, amount=amount, date=date)


def _pickup(db, n, cno, amount=350000.0, date="2026-03-28"):
    return _doc(db, f"提货单{n}.txt", "提货单", "提货单", cno=cno, amount=amount, date=date)


def test_auto_bundle_groups_contract_and_docs(db):
    """同一合同号的销售合同 + 发票 + 结算单 + 提货单 → 一个包、四个角色、齐备。"""
    cno = "XS-2026-1001"
    _sale(db, "A", cno)
    _invoice(db, "A", cno)
    _settle(db, "A", cno)
    _pickup(db, "A", cno)

    assert db.query(Bundle).count() == 1
    bundle = db.query(Bundle).first()
    assert bundle.contract_key == "XS20261001"
    assert bundle.direction == "sale"
    assert db.query(BundleItem).filter(BundleItem.bundle_id == bundle.id).count() == 4

    rep = svc.get_bundle_detail(db, bundle.id)
    assert rep["complete"] is True, rep["completeness"]
    assert rep["completeness"] == {"have": 3, "need": 3, "missing": []}
    assert [g["contract_no"] for g in rep["groups"]] == [cno]
    roles = {a["role"] for a in rep["groups"][0]["anchors"]}
    assert roles == {"销售合同"}
    assert {a["role"] for a in rep["groups"][0]["attachments"]} == {"发票", "结算单", "提货单"}


def test_missing_requirement_shown_in_report(db):
    """缺发票/缺提货单要能在包上一眼看到（齐备度 1/3）。"""
    cno = "XS-2026-1002"
    _sale(db, "B", cno)
    _settle(db, "B", cno)
    rep = svc.get_bundle_detail(db, db.query(Bundle).first().id)
    assert rep["complete"] is False
    assert rep["completeness"]["have"] == 1 and rep["completeness"]["need"] == 3
    assert sorted(rep["completeness"]["missing"]) == sorted(["发票", "提货单"])


def test_doc_without_contract_number_not_bundled(db):
    """无合同编号的文档不进包。"""
    _doc(db, "无号发票.txt", "发票", "发票", amount=1000.0)
    assert db.query(Bundle).count() == 0


def test_editing_contract_number_moves_item(db):
    """人工改合同编号 → 条目迁到新包，旧包空了就删。"""
    cno_a, cno_b = "XS-2026-2001", "XS-2026-2002"
    old_docs = [_sale(db, "C", cno_a)]
    inv = _invoice(db, "C", cno_a)
    assert db.query(Bundle).count() == 1  # 同编号 → 同一个包

    f = db.query(DocumentField).filter(DocumentField.document_id == inv.id,
                                       DocumentField.name == "合同编号").first()
    f.value = cno_b
    db.commit()
    svc.attach_document(db, inv)

    db.expire_all()
    old = db.query(Bundle).filter(Bundle.contract_key == "XS20262001").first()
    new = db.query(Bundle).filter(Bundle.contract_key == "XS20262002").first()
    assert new and db.query(BundleItem).filter(BundleItem.document_id == inv.id,
                                               BundleItem.bundle_id == new.id).count() == 1
    assert {i.document_id for i in svc.items_of(db, old.id)} == {old_docs[0].id}


def test_purging_document_cascades_item(db):
    """文档彻底删除 → 包条目随 FK 级联消失，不留幽灵。"""
    from backend.services.recycle import purge, to_recycle

    cno = "XS-2026-2003"
    _sale(db, "D", cno)
    inv = _invoice(db, "D", cno)
    bid = db.query(Bundle).first().id
    assert db.query(BundleItem).filter(BundleItem.bundle_id == bid).count() == 2

    item = to_recycle(db, inv)
    purge(db, item)
    db.expire_all()
    assert db.query(BundleItem).filter(BundleItem.document_id == inv.id).count() == 0
    assert db.query(Bundle).filter(Bundle.id == bid).count() == 1


def test_recycled_doc_excluded_from_completeness(db):
    """进了回收站的发票不该继续算作包内证据，恢复后又算回来。"""
    from backend.services.recycle import to_recycle

    cno = "XS-2026-2004"
    _sale(db, "E", cno)
    inv = _invoice(db, "E", cno)
    _settle(db, "E", cno)
    _pickup(db, "E", cno)
    bid = db.query(Bundle).first().id
    assert svc.get_bundle_detail(db, bid)["complete"] is True

    to_recycle(db, inv)
    db.expire_all()
    rep = svc.get_bundle_detail(db, bid)
    assert rep["complete"] is False
    assert rep["completeness"]["missing"] == ["发票"]

    # 从回收站恢复：状态回归档，齐备度也随之回来
    from backend.models import RecycleItem
    from backend.services.recycle import restore

    item = db.query(RecycleItem).filter(RecycleItem.document_id == inv.id).first()
    assert item.prev_status == STATUS_ARCHIVED
    restore(db, item)
    db.expire_all()
    assert db.get(Document, inv.id).status == STATUS_ARCHIVED
    rep = svc.get_bundle_detail(db, bid)
    assert rep["complete"] is True and rep["completeness"]["missing"] == [], rep["completeness"]


def test_manual_add_remove_and_role_override(db):
    """手工建包、加入/移出文档、改写角色。"""
    bundle = svc.create_bundle(db, "手工业务")
    assert bundle.source == "manual"
    inv = _invoice(db, "F", "XS-2026-2006")
    out = svc.add_documents(db, bundle, [inv.id])
    assert out["added"] == [inv.id]

    item = db.query(BundleItem).filter(BundleItem.document_id == inv.id).first()
    assert item.added_by == "manual"
    svc.set_item_role(db, item.id, "结算单")
    db.expire_all()
    assert db.get(BundleItem, item.id).role == "结算单"

    svc.remove_document(db, bundle, inv.id)
    db.expire_all()
    assert db.query(BundleItem).filter(BundleItem.document_id == inv.id).first() is None
    # 手工包不因清空而自动消失，auto 包才会
    assert db.get(Bundle, bundle.id) is not None

    with pytest.raises(svc.BundleError):
        svc.remove_document(db, bundle, inv.id)


def test_merge_moves_items_and_drops_source(db):
    a = svc.create_bundle(db, "A")
    b = svc.create_bundle(db, "B")
    d1 = _invoice(db, "G1", "XS-2026-2007")
    d2 = _invoice(db, "G2", "XS-2026-2008")
    svc.add_documents(db, a, [d1.id])
    svc.add_documents(db, b, [d2.id])

    out = svc.merge_bundles(db, a.id, b.id, note="同一笔业务")
    db.expire_all()
    assert out["moved"] == 1
    assert db.get(Bundle, a.id) is None
    assert {i.document_id for i in svc.items_of(db, b.id)} == {d1.id, d2.id}
    assert "同一笔业务" in db.get(Bundle, b.id).note
    from backend.models import OperationLog

    assert db.query(OperationLog).filter(OperationLog.op == "bundle_merge").count() == 1


def _make_pair(db, tag, *, sale_amt, purchase_amt, sale_date, purchase_date,
               entity="华东供应链", purchase_entity=None):
    """每个场景用独立主体名，避免跨场景互相配对。"""
    s = _doc(db, f"销售合同{tag}.txt", "合同", "销售合同", cno=f"XS-{tag}",
             amount=sale_amt, date=sale_date, company=f"{entity}{tag}", party="北方钢铁")
    p = _doc(db, f"采购合同{tag}.txt", "合同", "采购合同", cno=f"CG-{tag}",
             amount=purchase_amt, date=purchase_date,
             company=f"{purchase_entity or entity}{tag}", party="南方电子")
    return s, p


def test_suggestions_hit_and_miss(db):
    """合并建议：方向相反 + 我方主体一致 + 金额差 ≤5% + 日期差 ≤30 天，缺一不报。"""
    _make_pair(db, "3001", sale_amt=100000, purchase_amt=101000,
               sale_date="2026-05-10", purchase_date="2026-05-20")            # 命中
    _make_pair(db, "3002", sale_amt=100000, purchase_amt=80000,
               sale_date="2026-05-10", purchase_date="2026-05-20")            # 金额差 20%
    _make_pair(db, "3003", sale_amt=100000, purchase_amt=101000,
               sale_date="2026-05-10", purchase_date="2026-08-01")            # 日期差 83 天
    _make_pair(db, "3004", sale_amt=100000, purchase_amt=101000,
               sale_date="2026-05-10", purchase_date="2026-05-20",
               purchase_entity="别的公司")                                       # 主体不同
    # 方向相同（两份都是销售合同）不成对
    _s5, p5 = _make_pair(db, "3005", sale_amt=100000, purchase_amt=101000,
                         sale_date="2026-05-10", purchase_date="2026-05-20")
    item = db.query(BundleItem).filter(BundleItem.document_id == p5.id).first()
    svc.set_item_role(db, item.id, "销售合同")   # 改角色会连带重算方向
    db.expire_all()
    assert db.get(Bundle, item.bundle_id).direction == "sale"

    pairs = svc.suggestions(db)
    assert len(pairs) == 1, pairs
    hit = pairs[0]
    assert "XS-3001" in hit["sale_name"] and "CG-3001" in hit["purchase_name"]
    assert hit["date_gap_days"] == 10 and hit["amount_gap_pct"] <= 1.0
    assert hit["our_entity"] == "华东供应链3001"
    assert hit["sale_bundle_id"] != hit["purchase_bundle_id"]


def test_our_company_setting_splits_bundle_parties(db, monkeypatch):
    """本公司名称决定谁是「我方」：没填宁可留空，填了重建归集就分开。

    从前 fields 把第一个公司名同时写进两个字段，包上的客户和我方主体都是对方，
    合并建议拿我方主体比对永远相等，配对全错。
    """
    cno = "XS-2026-6301"
    _doc(db, "销售合同6301.txt", "合同", "销售合同", cno=cno,
         party="山西宏达钢铁有限公司", company="山西宏达钢铁有限公司")
    b = db.query(Bundle).filter(Bundle.contract_key == "XS20266301").first()
    assert b.customer == "山西宏达钢铁有限公司"
    assert b.our_entity == "", "分不清我方时不该把对方抄成我方主体"

    monkeypatch.setattr(settings, "OUR_COMPANY", "华山云创供应链科技有限公司")
    svc.rebuild_all(db)
    db.expire_all()
    b = db.get(Bundle, b.id)
    assert b.our_entity == "华山云创供应链科技有限公司"
    assert b.customer == "山西宏达钢铁有限公司"


def test_our_company_setting_survives_new_documents(db, monkeypatch):
    """填了本公司名称后新建的包直接带上正确的我方主体，不必再重建一次。"""
    monkeypatch.setattr(settings, "OUR_COMPANY", "华山云创供应链科技有限公司")
    cno = "XS-2026-6302"
    _doc(db, "销售合同6302.txt", "合同", "销售合同", cno=cno,
         party="山西宏达钢铁有限公司", company="山西宏达钢铁有限公司")
    b = db.query(Bundle).filter(Bundle.contract_key == "XS20266302").first()
    assert b.our_entity == "华山云创供应链科技有限公司"
    assert b.customer == "山西宏达钢铁有限公司"
    assert b.name == f"{cno} · 山西宏达钢铁有限公司"

def test_rebuild_backfills_history(db):
    """历史数据：删掉包后 rebuild_all 依据归档文档重新长出来，手工包不被抢。"""
    cno = "XS-2026-4001"
    sale = _sale(db, "H", cno)
    inv = _invoice(db, "H", cno)
    manual = svc.create_bundle(db, "手工保留")
    loose = _settle(db, "H9", "XS-2026-4999")     # 另一笔业务，手工归着
    svc.add_documents(db, manual, [loose.id])

    db.query(BundleItem).filter(BundleItem.bundle_id != manual.id).delete()
    db.query(Bundle).filter(Bundle.source == "auto").delete()
    db.commit()

    out = svc.rebuild_all(db)
    db.expire_all()
    assert out["attached"] == 2, out
    assert db.get(Bundle, manual.id) is not None
    autos = db.query(Bundle).filter(Bundle.source == "auto").all()
    assert len(autos) == 1
    assert {i.document_id for i in svc.items_of(db, autos[0].id)} == {sale.id, inv.id}
    # 手工包里的单子没被抢走
    assert {i.document_id for i in svc.items_of(db, manual.id)} == {loose.id}


def test_manual_bundle_is_home_for_same_contract(db):
    """手工包认领了某个合同号后，同编号的新归档文档直接进这个包，不再另起 auto 包。"""
    cno = "XS-2026-4002"
    manual = svc.create_bundle(db, "我在跟的单子")
    first = _invoice(db, "J", cno)
    svc.add_documents(db, manual, [first.id])
    db.expire_all()
    assert db.get(Bundle, manual.id).contract_key == "XS20264002"

    second = _settle(db, "J", cno)
    db.expire_all()
    assert db.query(Bundle).count() == 1
    assert {i.document_id for i in svc.items_of(db, manual.id)} == {first.id, second.id}


def test_bundles_api_crud(db, client):
    cno = "XS-2026-5001"
    doc = _sale(db, "I", cno)
    r = client.get("/api/bundles")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1 and body["items"][0]["contract_key"]
    assert body["items"][0]["complete"] is False

    r = client.post("/api/bundles", json={"name": "接口建包"})
    assert r.status_code == 200
    bid = r.json()["id"]
    r = client.post(f"/api/bundles/{bid}/items", json={"document_ids": [doc.id]})
    assert r.json()["added"] == [doc.id]
    r = client.patch(f"/api/bundles/{bid}", json={"status": "settled"})
    assert r.json()["status"] == "settled"
    r = client.get(f"/api/bundles/{bid}")
    assert r.json()["complete"] is False  # 只有合同、没有附件
    assert client.delete(f"/api/bundles/{bid}").status_code == 200
    assert client.get(f"/api/bundles/{bid}").status_code == 404
    assert client.post("/api/bundles/rebuild").status_code == 200
    assert client.get("/api/bundles/suggestions").status_code == 200


def test_auto_bundle_name_follows_the_contract_party(db, client):
    """先建档的往往是发票，客户名要等合同到货后跟着改对。"""
    cno = "XS-2026-1090"
    _invoice(db, "NAME", cno)          # 发票上没有对方单位，先建出来的包只能叫合同号
    b = db.query(Bundle).filter(Bundle.contract_key == "XS20261090").first()
    assert b and b.name == cno, b.name if b else "没有包"

    _sale(db, "NAME2", cno)
    db.expire_all()
    b = db.get(Bundle, b.id)
    assert b.name == f"{cno} · 北方钢铁贸易有限公司", b.name

    # 用户改过名字之后这个名字就归用户：重算与「重建归集」都不许再覆盖它
    r = client.patch(f"/api/bundles/{b.id}", json={"name": "北方钢铁钢材销售"})
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "manual"
    assert client.post("/api/bundles/rebuild").status_code == 200
    db.expire_all()
    assert "北方钢铁钢材销售" in [x.name for x in db.query(Bundle).all()]


def test_concurrent_attach_makes_one_bundle(db):
    """四条线程同时归档同一合同号的单据，也只能有一个业务包（旧版会把它劈成两半）。"""
    import threading

    from backend.database import SessionLocal

    cno = "XS-2026-1099"
    ids = [_sale(db, "C1", cno).id, _invoice(db, "C2", cno).id,
           _settle(db, "C3", cno).id, _pickup(db, "C4", cno).id]
    db.query(BundleItem).delete()
    db.query(Bundle).delete()
    db.commit()

    gate = threading.Barrier(len(ids))
    boom: list[Exception] = []

    def work(doc_id: int) -> None:
        with SessionLocal() as d:
            try:
                gate.wait(10)
                svc.attach_document(d, d.get(Document, doc_id))
            except Exception as exc:  # noqa: BLE001
                boom.append(exc)

    threads = [threading.Thread(target=work, args=[i]) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not boom, boom

    db.rollback()
    db.expire_all()
    bundles = db.query(Bundle).all()
    assert len(bundles) == 1, [b.contract_key for b in bundles]
    assert {i.role for i in svc.items_of(db, bundles[0].id)} == {
        "销售合同", "发票", "结算单", "提货单"}
    assert svc.bundle_report(db, bundles[0])["complete"] is True


def test_duplicate_key_bundles_are_folded_on_start(db):
    """老库里竞态留下的重复包：启动自愈并成一个，齐备度不再被对半砍。"""
    from sqlalchemy import text

    cno = "XS-2026-1100"
    sale = _sale(db, "D1", cno)
    inv = _invoice(db, "D2", cno)
    indexed = {r[0] for r in db.execute(text(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='bundles'"))}
    assert "uq_bundles_contract_key" in indexed

    # 模拟还没有唯一索引的旧库：手工造一个同合同号的分裂包（发票被关在另一个包里）
    db.execute(text("DROP INDEX uq_bundles_contract_key"))
    dup = Bundle(name="发票单号", contract_key="XS20261100", source="auto")
    db.add(dup)
    db.flush()
    # 竞态留下的样子：发票被装进了另一个同号包
    db.query(BundleItem).filter(BundleItem.document_id == inv.id).update(
        {"bundle_id": dup.id}, synchronize_session=False)
    db.commit()
    groups = svc.duplicate_key_groups(db)
    assert len(groups) == 1 and len(groups[0]) == 2, groups

    assert svc.repair_duplicate_bundles(db) == 1
    db.expire_all()
    left = db.query(Bundle).filter(Bundle.contract_key == "XS20261100").all()
    assert len(left) == 1
    assert {i.role for i in svc.items_of(db, left[0].id)} == {"销售合同", "发票"}

    # 并干净之后唯一索引才建得回来（init_db 的 _ensure_indexes 就是这一步）
    from backend.database import _ensure_indexes

    _ensure_indexes()
    again = {r[0] for r in db.execute(text(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='bundles'"))}
    assert "uq_bundles_contract_key" in again


def test_manual_bundle_can_take_over_a_contract_number(db):
    """手工包认领某个合同号时，占着同号的自动包要被吸过来，而不是撞唯一索引报错。"""
    cno = "XS-2026-1101"
    sale = _sale(db, "T1", cno)
    inv = _invoice(db, "T2", cno)
    manual = svc.create_bundle(db, "我在跟的单子")
    svc.add_documents(db, manual, [inv.id])

    db.expire_all()
    rows = db.query(Bundle).filter(Bundle.contract_key == "XS20261101").all()
    assert len(rows) == 1
    assert rows[0].id == manual.id
    assert {i.document_id for i in svc.items_of(db, manual.id)} == {sale.id, inv.id}


def test_settled_amount_is_bucketed_not_summed(db):
    """发票和结算单说的是同一笔钱：包头按单据类型取一桶，不能相加造出假差额。"""
    ok_cno, bad_cno = "XS-2026-1102", "XS-2026-1103"
    _sale(db, "BK1", ok_cno, amount=452000.0)
    _settle(db, "BK2", ok_cno, amount=452000.0)
    _invoice(db, "BK3", ok_cno, amount=452000.0)
    _sale(db, "BK4", bad_cno, amount=452000.0)
    _settle(db, "BK5", bad_cno, amount=452000.0)
    _invoice(db, "BK6", bad_cno, amount=300000.0)

    def group(cno: str) -> dict:
        key = cno.replace("XS-2026-", "XS2026")
        b = db.query(Bundle).filter(Bundle.contract_key == key).first()
        assert b, key
        return svc.bundle_report(db, b)["groups"][0]

    good = group(ok_cno)
    assert good["settlement_sum"] == 452000.0, good["settlement_sum"]
    assert good["amount_gap"] is None, good
    assert good["settlement_role"] in ("发票", "结算单")

    bad = group(bad_cno)
    assert bad["settlement_role"] == "发票", bad
    assert bad["settlement_sum"] == 300000.0 and bad["amount_gap"] == -152000.0, bad


def test_company_selfcheck_points_at_the_fix(db, monkeypatch):
    """没填本公司名称只提示；真的出现「客户==我方」脏包时才给一键修复。"""
    from backend.services.selfcheck import _check_company

    monkeypatch.setattr(settings, "OUR_COMPANY", "")
    cno = "XS-2026-6303"
    _doc(db, "销售合同6303.txt", "合同", "销售合同", cno=cno, party="山西宏达钢铁有限公司")
    info = _check_company()
    assert info["level"] == "info" and info["fix"] is None, info

    b = db.query(Bundle).filter(Bundle.contract_key == "XS20266303").first()
    b.our_entity = b.customer
    db.commit()
    warn = _check_company()
    assert warn["level"] == "warn" and warn["fix"] == "rebuild_bundles", warn

    monkeypatch.setattr(settings, "OUR_COMPANY", "华山云创供应链科技有限公司")
    ok = _check_company()
    assert ok["level"] == "ok" and ok["message"] == "华山云创供应链科技有限公司", ok
