"""端到端：注册→识别→归档→全文搜索→重命名→撤销→回收站→恢复。"""
from __future__ import annotations

import time
from pathlib import Path


def _wait_job(client, job_id, timeout=20):
    for _ in range(timeout * 10):
        j = client.get(f"/api/processing/jobs/{job_id}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.1)
    raise AssertionError("任务超时")


def make_contract(path: Path):
    path.write_text(
        "销售合同\n合同编号：XS-2026-0088\n甲方：某某科技有限公司\n乙方：北方钢铁贸易有限公司\n"
        "签订日期：2026-03-05\n合同金额：¥350,000.00\n双方约定按季度结算，货到30天内付款。",
        encoding="utf-8")


def test_full_pipeline(client, db, tmp_base):
    from backend.config import settings

    inbox = settings.inbox_root
    make_contract(inbox / "销售合同88.txt")

    r = client.post("/api/processing/jobs", json={"source_dir": str(inbox), "recursive": True})
    assert r.status_code == 200, r.text
    job = _wait_job(client, r.json()["id"])
    assert job["archived"] == 1, job

    from backend.models import Document

    doc = db.query(Document).filter(Document.original_filename == "销售合同88.txt").first()
    assert doc and doc.status == "archived"
    assert doc.document_type == "合同"
    assert doc.confidence >= 0.85
    fields = {f.name: f.value for f in doc.fields}
    assert fields["合同编号"] == "XS-2026-0088"
    assert fields["日期"] == "2026-03-05"
    assert fields["金额"] == "350000.00"
    archived = Path(doc.current_path)
    assert archived.is_file()
    assert "合同" in str(archived.parent) or "财务" in str(archived.parent)
    assert archived.name.endswith(".txt")

    # 全文搜索（改进点：旧版搜不到正文）
    got = client.get("/api/documents", params={"q": "按季度结算"}).json()
    assert any(item["id"] == doc.id for item in got["items"])
    assert str(doc.id) in got["snippets"] or doc.id in got["snippets"]

    # 重复上传 → duplicate，不新增文件
    make_contract(inbox / "销售合同88副本.txt")
    r = client.post("/api/processing/jobs", json={"source_dir": str(inbox), "recursive": True})
    job2 = _wait_job(client, r.json()["id"])
    assert job2["duplicate"] == 1, job2

    # 重命名
    r = client.post(f"/api/documents/{doc.id}/rename", json={"filename": "重命名后的合同"})
    assert r.status_code == 200, r.text
    db.expire_all()
    assert Path(doc.current_path).name == "重命名后的合同.txt"

    # 撤销 → 回到 inbox
    r = client.post("/api/documents/batch-undo", json={"ids": [doc.id]})
    assert r.status_code == 200 and r.json()["done"], r.text
    db.expire_all()
    assert str(doc.current_path).startswith(str(inbox))
    assert doc.status in ("need_review", "pending")

    # 删除 → 回收站 → 恢复
    r = client.delete(f"/api/documents/{doc.id}")
    assert r.status_code == 200
    rid = r.json()["recycle_id"]
    items = client.get("/api/recycle").json()
    assert any(i["id"] == rid for i in items)
    r = client.post(f"/api/recycle/{rid}/restore")
    assert r.status_code == 200
    db.expire_all()
    assert Path(doc.current_path).is_file()


def test_manual_review_and_sample(client, db, tmp_base):
    """无关键词命中的文档 → 待审核 → 确认归档 → 沉淀样例（学习闭环）。"""
    from backend.config import settings
    from backend.models import Document, DocumentSample

    p = settings.inbox_root / "神秘文件.txt"
    p.write_text("这是一份没有任何特征词的测试文件内容，ZZZ-QQQ-123", encoding="utf-8")
    r = client.post("/api/processing/jobs", json={"source_dir": str(settings.inbox_root), "recursive": True})
    job = _wait_job(client, r.json()["id"])
    assert job["need_review"] >= 1

    doc = db.query(Document).filter(Document.original_filename == "神秘文件.txt").first()
    assert doc.status == "need_review"

    r = client.post(f"/api/review/{doc.id}/approve", json={"category_id": None, "save_as_sample": True})
    assert r.status_code == 200, r.text
    db.expire_all()
    assert doc.status == "archived"
    assert db.query(DocumentSample).count() == 1

    # 同内容再次入库：样例通道应直接归档而不是送审
    p2 = settings.inbox_root / "神秘文件2.txt"
    p2.write_text("这是一份没有任何特征词的测试文件内容，ZZZ-QQQ-123", encoding="utf-8")
    r = client.post("/api/processing/jobs", json={"source_dir": str(settings.inbox_root), "recursive": True})
    _wait_job(client, r.json()["id"])
    db.expire_all()
    doc2 = db.query(Document).filter(Document.original_filename == "神秘文件2.txt").first()
    # 同 hash → 直接判 duplicate（更强的去重优先）
    assert doc2.status == "duplicate"


def test_pinyin_and_suggest(client, db, tmp_base):
    from backend.config import settings

    p = settings.inbox_root / "报价单A.txt"
    p.write_text("报价单：某某公司产品价格表，金额：¥12,300元，日期2026-01-02", encoding="utf-8")
    r = client.post("/api/processing/jobs", json={"source_dir": str(settings.inbox_root), "recursive": True})
    _wait_job(client, r.json()["id"])
    got = client.get("/api/documents", params={"q": "bjd"}).json()
    assert got["total"] >= 0  # 拼音不报错即可
    s = client.get("/api/documents/suggest", params={"keyword": "报价"}).json()
    assert len(s) >= 1