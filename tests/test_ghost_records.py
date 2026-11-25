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
    assert client.get("/api/documents", params={"page": 1, "page_size": 50}).json()["items"]
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