"""文件已被外部删除的「幽灵记录」：必须能移入回收站 / 彻底删除，不能卡死在库里。"""
from __future__ import annotations

from pathlib import Path

import pytest


def _ghost(db, status="pending", name="失效合同.pdf", stale=None):
    """造一条 current_path 指向不存在文件的记录。"""
    from backend.models import Document

    gone = Path(str(Path.home() / "docflow_should_not_exist" / name)) if stale is None else Path(stale)
    d = Document(original_filename=name, current_filename=name,
                 original_path=str(gone), current_path=str(gone), file_type="pdf",
                 file_size=1234, document_type="合同", status=status, confidence=0.86)
    db.add(d)
    db.commit()
    return d, gone


def test_delete_missing_file_document_succeeds(client, db):
    d, _ = _ghost(db)
    r = client.delete(f"/api/documents/{d.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["file_missing"] is True
    db.refresh(d)
    assert d.status == "recycled"


def test_missing_record_appears_in_recycle_as_lost_file(client, db):
    from backend.models import RecycleItem

    d, gone = _ghost(db)
    recycle_id = client.delete(f"/api/documents/{d.id}").json()["recycle_id"]
    items = client.get("/api/recycle").json()
    assert len(items) == 1
    it = items[0]
    assert it["id"] == recycle_id
    assert it["file_missing"] is True
    assert "磁盘文件已丢失" in it["reason"]
    assert it["original_path"] == str(gone)
    row = db.get(RecycleItem, recycle_id)
    assert row.recycled_path == ""      # 无实体文件，只留元数据


def test_restore_lost_record_is_clear_and_purge_works(client, db):
    from backend.models import Document, RecycleItem

    d, _ = _ghost(db)
    doc_id = d.id
    item_id = client.delete(f"/api/documents/{doc_id}").json()["recycle_id"]
    r = client.post(f"/api/recycle/{item_id}/restore")
    assert r.status_code == 400
    assert "没有可恢复的文件" in r.json()["detail"]
    assert client.post(f"/api/recycle/{item_id}/purge", params={"confirm": True}).status_code == 200
    assert db.query(Document).filter(Document.id == doc_id).first() is None
    assert db.query(RecycleItem).filter(RecycleItem.id == item_id).first() is None


def test_batch_delete_handles_missing_files(client, db):
    from backend.config import settings
    from backend.models import Document

    d1, _ = _ghost(db, name="幽灵1.pdf")
    real = settings.document_root / "还在.pdf"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_bytes(b"%PDF-1.4 fake")
    d2 = Document(original_filename=real.name, current_filename=real.name,
                  original_path=str(real), current_path=str(real), file_type="pdf",
                  status="archived")
    db.add(d2)
    db.commit()

    r = client.post("/api/documents/batch-delete", json={"ids": [d1.id, d2.id], "reason": "整理清理"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["done"]) == sorted([d1.id, d2.id])
    assert body["errors"] == []
    assert body["file_missing"] == [d1.id]
    assert real.exists() is False       # 有文件的仍然正常搬进回收站


def test_recycle_falls_back_to_original_path(client, db):
    """current_path 失效但原件还在（原地整理留下的旧路径）：搬真实存在的那个文件。"""
    from backend.config import settings
    from backend.models import Document

    real = settings.inbox_root / "原件.pdf"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_bytes(b"%PDF-1.4 real")
    d = Document(original_filename=real.name, current_filename=real.name,
                 original_path=str(real), current_path=str(settings.document_root / "已被移走.pdf"),
                 file_type="pdf", status="pending")
    db.add(d)
    db.commit()

    assert client.delete(f"/api/documents/{d.id}").status_code == 200
    it = client.get("/api/recycle").json()[0]
    assert it["file_missing"] is False
    assert real.exists() is False
    assert Path(it["original_path"]) == real


def test_recycled_document_still_previewable(client, db):
    """软删除后 current_path 要跟到回收站，否则预览/缩略图一律 404。"""
    from backend.config import settings
    from backend.models import Document

    real = settings.document_root / "待回收.pdf"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_bytes(b"%PDF-1.4 recycled")
    d = Document(original_filename=real.name, current_filename=real.name,
                 original_path=str(real), current_path=str(real), file_type="pdf",
                 status="archived")
    db.add(d)
    db.commit()

    assert client.delete(f"/api/documents/{d.id}").status_code == 200
    db.expire_all()  # 接口用的是另一个 session，这里要重新取
    row = db.get(Document, d.id)
    assert Path(row.current_path).is_file()
    assert Path(row.current_path).resolve().is_relative_to(Path(settings.recycle_root).resolve())
    assert client.get(f"/api/documents/{d.id}").json()["path"] == row.current_path
    # 已回收的不在文档库显示（回收站才是它的家），但记录与路径都还在
    assert client.get("/api/documents", params={"page": 1, "page_size": 50}).json()["items"] == []
    it = client.get("/api/recycle").json()[0]
    assert it["file_missing"] is False
    # 原位置仍然记得，恢复时能回原
    assert Path(it["original_path"]) == real

    r = client.post(f"/api/recycle/{it['id']}/restore")
    assert r.status_code == 200, r.text
    assert real.is_file()
    db.expire_all()
    assert db.get(Document, d.id).current_path == str(real)


def test_library_flags_missing_files(client, db):
    d, _ = _ghost(db)
    ok = client.get("/api/documents", params={"page": 1, "page_size": 50}).json()
    row = next(x for x in ok["items"] if x["id"] == d.id)
    assert row["file_exists"] is False


def test_selfcheck_detects_and_prunes_ghosts(client, db):
    ghost, _ = _ghost(db, name="自检幽灵.pdf")
    from backend.config import settings
    from backend.models import Document

    real = settings.document_root / "自检正常.pdf"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_bytes(b"%PDF-1.4 ok")
    keep = Document(original_filename=real.name, current_filename=real.name,
                    original_path=str(real), current_path=str(real), file_type="pdf",
                    status="archived")
    db.add(keep)
    db.commit()

    panel = client.get("/api/system/selfcheck").json()
    item = next(i for i in panel["items"] if i["key"] == "ghosts")
    assert item["level"] == "warn"
    assert item["fix"] == "prune_missing"
    assert str(ghost.id) in item["detail"]

    rep = client.post("/api/system/repair/prune_missing")
    assert rep.status_code == 200, rep.text
    assert "1 条" in rep.json()["detail"]
    assert ghost.id in [i["id"] for i in client.get("/api/recycle").json()]
    assert client.get(f"/api/documents/{keep.id}").json()["status"] == "archived"
    assert real.exists()

    # quick 自检（顶栏轮询）不做全盘 stat，避免拖慢界面
    quick = client.get("/api/system/status").json()
    assert "ghosts" not in {i["key"] for i in quick["issues"]}


def test_purge_expired_cleans_lost_records(client, db):
    from datetime import datetime, timedelta

    from backend.database import SessionLocal
    from backend.models import Document, RecycleItem
    from backend.services.recycle import purge_expired

    d, _ = _ghost(db, name="过期幽灵.pdf")
    client.delete(f"/api/documents/{d.id}")
    with SessionLocal() as sdb:
        it = sdb.query(RecycleItem).one()
        it.expires_at = datetime.now() - timedelta(minutes=1)
        sdb.commit()
        assert purge_expired(sdb) == 1
        assert sdb.get(Document, d.id) is None


def _archived_pdf(db, name="回收隐身.pdf", cat_name="发票", doc_type="发票"):
    """一份真的在归档位置、状态已归档的文档。"""
    from backend.config import settings
    from backend.models import Category, Document

    real = settings.document_root / name
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_bytes(b"%PDF-1.4 recycled")
    cat = db.query(Category).filter(Category.name == cat_name).first()
    d = Document(original_filename=name, current_filename=name,
                 original_path=str(real), current_path=str(real), file_type="pdf",
                 document_type=doc_type, status="archived",
                 archived_category_id=cat.id if cat else None, confidence=0.9)
    db.add(d)
    db.commit()
    return d, real


def _leaf(tree, name):
    for n in tree:
        if n["name"] == name:
            return n
        hit = _leaf(n.get("children") or [], name)
        if hit:
            return hit
    return None


def test_recycled_document_leaves_the_library(client, db):
    """移入回收站 = 从文档库消失：列表、类型下拉、目录计数、看板都不再算它。"""
    d, _ = _archived_pdf(db)
    assert d.id in [x["id"] for x in client.get("/api/documents").json()["items"]]

    client.delete(f"/api/documents/{d.id}")
    body = client.get("/api/documents").json()
    assert body["items"] == [] and body["total"] == 0, body
    assert body["recycled_hidden"] == 1, "隐身了也要告诉用户去哪找，别让人以为被物理删除"
    assert d.id in [i["document_id"] for i in client.get("/api/recycle").json()]

    seen = client.get("/api/documents", params={"status": "recycled"}).json()
    assert [x["id"] for x in seen["items"]] == [d.id]
    assert seen["recycled_hidden"] == 0

    assert client.get("/api/documents", params={"q": "回收隐身"}).json()["items"] == []
    assert client.get("/api/documents", params={"document_type": "发票"}).json()["items"] == []
    assert "发票" not in client.get("/api/documents/types").json()
    assert client.get("/api/documents/suggest", params={"keyword": "回收隐身"}).json() == []
    assert _leaf(client.get("/api/categories/tree").json(), "发票")["doc_count"] == 0
    assert client.get("/api/dashboard/overview").json()["total"] == 0


def test_restore_returns_doc_to_library_and_old_status(client, db):
    """恢复要回到进回收站之前的状态：已归档的回归档，不用重新整理一遍。"""
    from backend.models import Document

    d, real = _archived_pdf(db, name="恢复回归档.pdf")
    item_id = client.delete(f"/api/documents/{d.id}").json()["recycle_id"]
    assert not real.exists()

    r = client.post(f"/api/recycle/{item_id}/restore")
    assert r.status_code == 200, r.text
    db.expire_all()
    row = db.get(Document, d.id)
    assert row.status == "archived", row.status
    assert Path(row.current_path) == real and real.is_file()
    assert d.id in [x["id"] for x in client.get("/api/documents").json()["items"]]
    assert client.get("/api/documents").json()["recycled_hidden"] == 0
    assert client.get("/api/dashboard/overview").json()["total"] == 1


def test_restore_pending_doc_stays_pending(client, db):
    """待整理的文档回收再恢复，不该被凭空升级成已归档。"""
    from backend.config import settings
    from backend.models import Document

    p = settings.inbox_root / "恢复待整理.pdf"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"%PDF-1.4 x")
    d = Document(original_filename=p.name, current_filename=p.name, original_path=str(p),
                 current_path=str(p), file_type="pdf", status="pending")
    db.add(d)
    db.commit()
    iid = client.delete(f"/api/documents/{d.id}").json()["recycle_id"]
    assert client.get("/api/recycle").json()[0]["prev_status"] == "pending"
    client.post(f"/api/recycle/{iid}/restore")
    db.expire_all()
    assert db.get(Document, d.id).status == "pending"


def test_cannot_undo_while_recycled(client, db):
    """撤销会把文件从回收站里搬走，却留下一个恢复不了的回收记录——必须先恢复。"""
    from backend.models import Document

    d, _ = _archived_pdf(db, name="回收中撤销.pdf")
    from backend.config import settings
    from backend.models import OperationLog

    db.add(OperationLog(document_id=d.id, op="archive", result="ok",
                       old_path=str(settings.inbox_root / d.original_filename),
                       new_path=str(Path(d.current_path))))
    db.commit()
    iid = client.delete(f"/api/documents/{d.id}").json()["recycle_id"]

    r = client.post("/api/documents/batch-undo", json={"ids": [d.id]})
    assert r.status_code == 200 and not r.json()["done"], r.text
    assert "回收站" in str(r.json()["errors"]), r.json()
    db.expire_all()
    assert db.get(Document, d.id).status == "recycled"

    assert client.post(f"/api/recycle/{iid}/restore").status_code == 200
    db.expire_all()
    out = client.post("/api/documents/batch-undo", json={"ids": [d.id]}).json()
    assert [x["id"] for x in out["done"]] == [d.id], out


def test_legacy_recycle_row_restores_to_pending(client, db):
    """老库的回收记录没有 prev_status：退回待整理，不要报错，也不要谎称已归档。"""
    from backend.models import Document, RecycleItem

    d, _ = _archived_pdf(db, name="老记录恢复.pdf")
    iid = client.delete(f"/api/documents/{d.id}").json()["recycle_id"]
    with db.no_autoflush:
        db.get(RecycleItem, iid).prev_status = ""
        db.commit()
    assert client.post(f"/api/recycle/{iid}/restore").status_code == 200
    db.expire_all()
    assert db.get(Document, d.id).status == "pending"
