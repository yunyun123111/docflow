"""人工审核 API：待审列表 / 指纹聚类 / 确认归档 / 批量 / 跳过 / 学习建议。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.config import settings
from backend.models import (
    Category, Document, DocumentSample, RenameTemplate, STATUS_NEED_REVIEW, STATUS_PENDING,
)
from backend.schemas import BatchApproveRequest, ClusterConfirmRequest, ReviewApproveRequest
from backend.services.naming import render_name, render_path, template_for
from backend.services.pipeline import pipeline
from backend.utils.fs import safe_filename
from backend.utils.logger import get_logger

router = APIRouter(prefix="/api/review", tags=["review"])
logger = get_logger("api.review")


@router.get("")
def list_review(db: Session = Depends(get_db)):
    rows = db.query(Document).filter(Document.status == STATUS_NEED_REVIEW) \
        .order_by(Document.confidence.asc().nulls_last(), Document.id.desc()).all()
    return [{
        "id": d.id, "filename": d.current_filename or d.original_filename,
        "document_type": d.document_type, "confidence": d.confidence,
        "confidence_detail": d.confidence_detail,
        "suggested_category_id": d.suggested_category_id,
        "fingerprint": d.fingerprint, "has_seal": d.has_seal,
        "fields": {f.name: f.value for f in d.fields},
        "created_at": d.created_at.isoformat(timespec="seconds"),
    } for d in rows]


@router.get("/clusters")
def clusters(db: Session = Depends(get_db)):
    """按指纹聚类待审文档（同模板批量处理入口）。"""
    rows = db.query(Document).filter(Document.status == STATUS_NEED_REVIEW).all()
    groups: dict[str, list] = {}
    for d in rows:
        groups.setdefault(d.fingerprint or f"doc-{d.id}", []).append(d)
    out = []
    for fp, ds in groups.items():
        ds = sorted(ds, key=lambda x: x.id)
        base = ds[0]
        out.append({
            "fingerprint": fp, "count": len(ds),
            "ids": [d.id for d in ds],
            "base_id": base.id,
            "base_filename": safe_filename(Path(base.original_filename).stem, Path(base.current_path or base.original_path).suffix),
            "document_type": base.document_type,
            "min_confidence": min(d.confidence or 0 for d in ds),
            "fields": {f.name: f.value for f in base.fields},
        })
    return sorted(out, key=lambda g: -g["count"])


@router.get("/samples", summary="样例库")
def list_samples(db: Session = Depends(get_db)):
    return [{"id": x.id, "name": x.name, "document_type": x.document_type,
             "category_id": x.category_id,
             "created_at": x.created_at.isoformat(timespec="seconds")}
            for x in db.query(DocumentSample).order_by(DocumentSample.id.desc()).all()]


@router.delete("/samples/{sample_id}", summary="删除样例")
def delete_sample(sample_id: int, db: Session = Depends(get_db)):
    x = db.get(DocumentSample, sample_id)
    if not x:
        raise HTTPException(404, "样例不存在")
    import os
    from pathlib import Path

    if x.path and Path(x.path).is_file():
        try:
            os.remove(x.path)
        except OSError:
            pass
    db.delete(x)
    db.commit()
    return {"ok": True}


@router.get("/{doc_id}")
def review_detail(doc_id: int, db: Session = Depends(get_db)):
    from backend.api.documents import get_document

    return get_document(doc_id, db)


def _approve_one(db: Session, doc: Document, category_id: int | None, doc_type: str | None,
                 base_name: str | None, rename_template_id: int | None,
                 save_sample: bool) -> dict:
    if doc.status != STATUS_NEED_REVIEW:
        raise HTTPException(400, f"文档状态不是待审核: {doc.status}")
    old_cat = doc.suggested_category_id
    if category_id is not None:
        doc.suggested_category_id = category_id
        from backend.services.learning import record_correction

        record_correction(db, doc, old_cat, category_id)
    if doc_type:
        doc.document_type = doc_type
    cat = db.get(Category, doc.suggested_category_id) if doc.suggested_category_id else None
    category_path = cat.path if cat else ""
    fields_map = {f.name: (f.value, f.confidence) for f in doc.fields}
    ext = Path(doc.current_path or doc.original_path).suffix
    if base_name:
        new_name = safe_filename(base_name, ext)
    elif rename_template_id:
        tpl = db.get(RenameTemplate, rename_template_id)
        pattern = tpl.pattern if tpl else settings.RENAME_TEMPLATE
        new_name = render_name(pattern, fields_map, ext, doc.document_type, category_path)
    else:
        tpl = template_for(db, doc.document_type)
        new_name = render_name(tpl.pattern, fields_map, ext, doc.document_type, category_path,
                               original=Path(doc.original_filename).stem)
    segments = render_path(settings.ARCHIVE_TEMPLATE, fields_map, doc.document_type, category_path)
    pipeline.apply_archive(db, doc, segments, new_name)
    if save_sample:
        _save_sample(db, doc)
    logger.info("审核确认 #%s → %s", doc.id, doc.current_path)
    return {"id": doc.id, "path": doc.current_path}


def _save_sample(db: Session, doc: Document) -> None:
    from backend.models import DocumentText
    from backend.services.fingerprint import fingerprint as make_fp

    text_row = db.get(DocumentText, doc.id)
    norm = make_fp(text_row.text if text_row else "")
    if db.query(DocumentSample).filter(DocumentSample.text_norm == norm,
                                       DocumentSample.document_type == doc.document_type).first():
        return
    sample_dir = db_sample_dir()
    src = Path(doc.current_path)
    target = sample_dir / f"sample_{doc.id}_{src.name}"
    if src.is_file() and not target.exists():
        import shutil

        shutil.copy2(str(src), str(target))
    db.add(DocumentSample(name=src.name, path=str(target), document_type=doc.document_type,
                          category_id=doc.suggested_category_id, text_norm=norm))
    db.commit()


def db_sample_dir() -> Path:
    from backend.config import settings

    settings.sample_root.mkdir(parents=True, exist_ok=True)
    return settings.sample_root


@router.post("/{doc_id}/approve")
def approve(doc_id: int, req: ReviewApproveRequest, db: Session = Depends(get_db)):
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    result = _approve_one(db, doc, req.category_id, req.document_type, req.filename,
                          req.rename_template_id, req.save_as_sample)
    db.commit()
    return {"ok": True, **result}


@router.post("/batch-approve")
def batch_approve(req: BatchApproveRequest, db: Session = Depends(get_db)):
    done, errors = [], []
    for i in req.ids:
        doc = db.get(Document, i)
        if not doc:
            errors.append({"id": i, "error": "不存在"})
            continue
        try:
            done.append(_approve_one(db, doc, req.category_id, None, None, None, False))
        except HTTPException as e:
            errors.append({"id": i, "error": e.detail})
    db.commit()
    return {"done": done, "errors": errors}


@router.post("/confirm-cluster")
def confirm_cluster(req: ClusterConfirmRequest, db: Session = Depends(get_db)):
    """指纹批次一键确认：base 文件名可带 {index} 占位批量编号。"""
    ids = req.ids or [r[0] for r in db.query(Document.id)
                      .filter(Document.fingerprint == req.fingerprint,
                              Document.status == STATUS_NEED_REVIEW).all()]
    done, errors = [], []
    for n, doc_id in enumerate(ids, start=1):
        doc = db.get(Document, doc_id)
        if not doc or doc.status != STATUS_NEED_REVIEW:
            errors.append({"id": doc_id, "error": "状态已变化"})
            continue
        base = req.base_filename.replace("{index}", f"{n:02d}") if req.base_filename else None
        try:
            done.append(_approve_one(db, doc, req.category_id, req.document_type, base, None, False))
        except HTTPException as e:
            errors.append({"id": doc_id, "error": e.detail})
    db.commit()
    return {"done": done, "errors": errors}


@router.post("/{doc_id}/skip")
def skip(doc_id: int, db: Session = Depends(get_db)):
    """跳过：保留 pending，不归档不删除。"""
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    doc.status = STATUS_PENDING
    db.commit()
    return {"ok": True}