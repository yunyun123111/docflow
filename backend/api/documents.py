"""文档 API：上传/列表(搜索+过滤)/详情/取文件/修正/重命名/删除/批量/导出。"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import get_db
from backend.models import (
    Category, Document, DocumentField, DocumentText, OperationLog,
    STATUS_ARCHIVED, STATUS_DUPLICATE, STATUS_NEED_REVIEW, STATUS_RECYCLED,
)
from backend.schemas import (
    BatchDeleteRequest, BatchIds, DocumentPatchRequest, ExportZipRequest, RenameRequest,
)
from backend.services import search as search_service
from backend.services.archive import sha256_file
from backend.services.parser import MIME, get_file_type
from backend.services.pipeline import pipeline
from backend.services.recycle import doc_file_missing, to_recycle
from backend.services.uploads import stage_uploads
from backend.services.undo import undo_document
from backend.utils.fs import fs_path, safe_filename, unique_filename
from backend.utils.logger import get_logger

router = APIRouter(prefix="/api/documents", tags=["documents"])
logger = get_logger("api.documents")

CHUNK = 1 << 20


def _brief(d: Document) -> dict:
    return {
        "id": d.id, "original_filename": d.original_filename,
        "current_filename": d.current_filename or d.original_filename,
        "file_type": d.file_type, "file_size": d.file_size,
        "document_type": d.document_type, "title": d.title,
        "status": d.status, "confidence": d.confidence,
        "suggested_category_id": d.suggested_category_id,
        "archived_category_id": d.archived_category_id,
        "has_seal": d.has_seal, "fingerprint": d.fingerprint,
        "created_at": d.created_at.isoformat(timespec="seconds") if d.created_at else None,
        "processed_at": d.processed_at.isoformat(timespec="seconds") if d.processed_at else None,
        "archived_at": d.archived_at.isoformat(timespec="seconds") if d.archived_at else None,
        "error": d.error[:200] if d.error else "",
        "file_exists": not doc_file_missing(d),
    }


@router.post("/upload")
async def upload_documents(request: Request, files: list[UploadFile] = File(...),
                           db: Session = Depends(get_db)):
    """流式写盘 + 大小上限 + 稳定性延迟；随后异步整理。"""
    saved, rejected = await stage_uploads(files)
    if not saved:
        return {"saved": [], "rejected": rejected, "job": None}
    # 稍等落盘稳定（MIN_FILE_AGE）后再入库；大文件由 stability 在 pipeline 处理时二次校验
    job = pipeline.start_files_job(db, saved, kind="upload", source="upload",
                                   hints={})
    return {"saved": [p.name for p in saved], "rejected": rejected,
            "job": {"id": job.id, "total": job.total}}


def _recycled_hidden(db: Session, common: list) -> int:
    """同一批筛选条件下被文档库隐藏掉的回收站记录数（让人知道东西没丢，只是搬家了）。"""
    from sqlalchemy import func

    return db.query(func.count(Document.id)).filter(*common,
                                                    Document.status == STATUS_RECYCLED).scalar() or 0


@router.get("")
def list_documents(db: Session = Depends(get_db),
                   status: str | None = None, document_type: str | None = None,
                   category_id: int | None = None, file_type: str | None = None,
                   q: str = "", date_from: str = "", date_to: str = "",
                   limit: int = Query(100, le=500), offset: int = 0):
    """文档库列表。已回收的记录默认不出现——回收站才是它们的家，恢复后自动回到这里；
    需要按状态看它们在库里长什么样时，显式传 `status=recycled`。"""
    common = []
    snippets: dict[int, str] = {}
    if q:
        ids, snippets = search_service.search_ids(db, q)
        if not ids:
            return {"items": [], "total": 0, "snippets": {}, "recycled_hidden": 0}
        common.append(Document.id.in_(ids))
    if document_type:
        common.append(Document.document_type == document_type)
    if file_type:
        common.append(Document.file_type == file_type)
    if category_id:
        common.append((Document.archived_category_id == category_id)
                      | (Document.suggested_category_id == category_id))
    if date_from:
        common.append(Document.created_at >= f"{date_from} 00:00:00")
    if date_to:
        common.append(Document.created_at <= f"{date_to} 23:59:59")
    wanted = [s for s in (status or "").split(",") if s]
    scoped = db.query(Document).filter(*common)
    if wanted:
        query = scoped.filter(Document.status.in_(wanted))
    else:
        query = scoped.filter(Document.status != STATUS_RECYCLED)
    total = query.count()
    rows = query.order_by(Document.id.desc()).offset(offset).limit(limit).all()
    return {"items": [_brief(d) for d in rows], "total": total, "snippets": snippets,
            "recycled_hidden": 0 if STATUS_RECYCLED in wanted else _recycled_hidden(db, common)}


@router.get("/suggest")
def suggest(keyword: str, db: Session = Depends(get_db)):
    ids, _ = search_service.search_ids(db, keyword)
    rows = db.query(Document).filter(Document.id.in_(ids[:20]),
                                     Document.status != STATUS_RECYCLED).all() if ids else []
    return [{"id": d.id, "name": d.current_filename or d.original_filename,
             "type": d.document_type, "status": d.status} for d in rows]


@router.get("/types")
def list_types(db: Session = Depends(get_db)):
    rows = db.query(Document.document_type) \
        .filter(Document.document_type != "", Document.status != STATUS_RECYCLED) \
        .distinct().order_by(Document.document_type).all()
    return [r[0] for r in rows]


@router.get("/stats")
def status_stats(db: Session = Depends(get_db)):
    from sqlalchemy import func

    rows = db.query(Document.status, func.count(Document.id)).group_by(Document.status).all()
    return dict(rows)


@router.get("/{doc_id}")
def get_document(doc_id: int, db: Session = Depends(get_db)):
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    out = _brief(doc)
    out["fields"] = [{"name": f.name, "value": f.value, "source": f.source,
                      "confidence": f.confidence} for f in doc.fields]
    text_row = db.get(DocumentText, doc_id)
    body = text_row.text if text_row else ""
    out["text_preview"] = body[:4000]
    out["text_length"] = len(body)
    out["confidence_detail"] = doc.confidence_detail
    out["path"] = doc.current_path or doc.original_path
    out["operations"] = [
        {"id": r.id, "op": r.op, "detail": r.detail, "result": r.result,
         "created_at": r.created_at.isoformat(timespec="seconds"), "undone": bool(r.undone_at)}
        for r in db.query(OperationLog).filter(OperationLog.document_id == doc_id)
        .order_by(OperationLog.id.desc()).limit(30).all()
    ]
    if doc.suggested_category_id:
        cat = db.get(Category, doc.suggested_category_id)
        out["suggested_category_path"] = cat.path if cat else ""
    return out


@router.get("/{doc_id}/preview")
def preview_document(doc_id: int, db: Session = Depends(get_db)):
    """兼容旧路径：预览逻辑已统一到 /api/preview（services/preview.py 负责渲染与守卫）。"""
    from backend.api.preview import preview_meta

    return preview_meta(doc_id, db)


@router.get("/{doc_id}/file")
def get_document_file(doc_id: int, db: Session = Depends(get_db), inline: bool = False):
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    p = Path(doc.current_path or doc.original_path)
    if not p.is_file():
        raise HTTPException(404, "磁盘文件不存在")
    from backend.api.preview import _media_type

    disp = "inline" if inline else "attachment"
    return FileResponse(fs_path(p), filename=p.name, content_disposition_type=disp,
                        media_type=_media_type(doc, p))


@router.patch("/{doc_id}")
def patch_document(doc_id: int, req: DocumentPatchRequest, db: Session = Depends(get_db)):
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    from backend.models import STATUS_RECYCLED

    if doc.status == STATUS_RECYCLED:
        raise HTTPException(400, "文档在回收站中，请先恢复")
    if req.document_type is not None:
        doc.document_type = req.document_type
    if req.title is not None:
        doc.title = req.title[:500]
    if req.suggested_category_id is not None:
        old_cat = doc.suggested_category_id
        doc.suggested_category_id = req.suggested_category_id
        from backend.services.learning import record_correction

        record_correction(db, doc, old_cat, req.suggested_category_id)
    for name, value in (req.fields or {}).items():
        f = db.query(DocumentField).filter(DocumentField.document_id == doc_id,
                                           DocumentField.name == name).first()
        if f:
            f.value = value[:500]
            f.source = "manual"
            f.confidence = 1.0
        else:
            db.add(DocumentField(document_id=doc_id, name=name[:80], value=value[:500],
                                 source="manual", confidence=1.0))
    if req.filename:
        pipeline.rename(db, doc, req.filename)
    db.commit()
    if req.fields or req.document_type is not None or req.suggested_category_id is not None:
        try:
            from backend.services import bundles as bundle_service

            bundle_service.attach_document(db, doc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("修正后重新入包失败 doc=%s: %s", doc_id, exc)
    logger.info("修正文档 #%s", doc_id)
    return get_document(doc_id, db)


@router.post("/{doc_id}/rename")
def rename_document(doc_id: int, req: RenameRequest, db: Session = Depends(get_db)):
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    new_name = pipeline.rename(db, doc, req.filename)
    return {"ok": True, "filename": new_name, "path": doc.current_path}


@router.post("/{doc_id}/reprocess")
def reprocess_document(doc_id: int, db: Session = Depends(get_db)):
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    p = Path(doc.current_path or doc.original_path)
    if not p.is_file():
        raise HTTPException(400, "磁盘文件不存在，无法重新识别")
    doc.status = "pending"
    doc.error = ""
    db.commit()
    # in_place：原地重跑这一条记录，别让它被自己的 sha256 判成重复
    job = pipeline.start_files_job(db, [p], kind="reprocess", source=f"doc:{doc_id}", in_place=True)
    return {"ok": True, "job_id": job.id}


@router.delete("/{doc_id}")
def delete_document(doc_id: int, db: Session = Depends(get_db)):
    """软删除 → 回收站（绝不物理删除）。"""
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    from backend.models import STATUS_RECYCLED

    if doc.status == STATUS_RECYCLED:
        raise HTTPException(400, "已在回收站")
    from backend.services.recycle import RecycleError

    try:
        item = to_recycle(db, doc, reason="手动删除")
    except RecycleError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "recycle_id": item.id, "file_missing": not item.recycled_path,
            "expires_at": item.expires_at.isoformat(timespec="seconds") if item.expires_at else None}


@router.post("/batch-undo")
def batch_undo(req: BatchIds, db: Session = Depends(get_db)):
    done, errors = [], []
    for doc_id in req.ids:
        doc = db.get(Document, doc_id)
        if not doc:
            errors.append({"id": doc_id, "error": "不存在"})
            continue
        try:
            n = undo_document(db, doc)
            done.append({"id": doc_id, "steps": n})
        except Exception as e:  # noqa: BLE001
            db.rollback()
            errors.append({"id": doc_id, "error": str(e)})
    return {"done": done, "errors": errors}


@router.post("/batch-delete")
def batch_delete(req: BatchDeleteRequest, db: Session = Depends(get_db)):
    from backend.services.recycle import RecycleError, to_recycle as _tr

    done, errors, missing = [], [], []
    for doc_id in req.ids:
        doc = db.get(Document, doc_id)
        if not doc or doc.status == "recycled":
            errors.append({"id": doc_id, "error": "不存在或已在回收站"})
            continue
        try:
            item = _tr(db, doc, reason=req.reason)
            done.append(doc_id)
            if not item.recycled_path:
                missing.append(doc_id)
        except RecycleError as e:
            errors.append({"id": doc_id, "error": str(e)})
    return {"done": done, "errors": errors, "file_missing": missing}


@router.post("/export-zip")
def export_zip(req: ExportZipRequest, db: Session = Depends(get_db)):
    """流式打包到临时文件再下发，避免整包进内存。"""
    import zipfile

    safe_zip = re.sub(r'[^0-9a-zA-Z\u4e00-\u9fff_\-. ]', "_", req.zip_name or "documents")[:60]
    tmp = settings.temp_dir / f"export_{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_zip}.zip"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    seen_names: set[str] = set()
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for doc_id in req.ids:
            doc = db.get(Document, doc_id)
            if not doc:
                continue
            p = Path(doc.current_path or doc.original_path)
            if not p.is_file():
                continue
            arc = doc.current_filename or p.name
            while arc in seen_names:
                arc = f"({written})_{arc}"
            seen_names.add(arc)
            zf.write(p, arc)
            written += 1
    if not written:
        tmp.unlink(missing_ok=True)
        raise HTTPException(404, "没有可导出的文件")

    def _stream():
        try:
            with open(tmp, "rb") as f:
                for chunk in iter(lambda: f.read(CHUNK), b""):
                    yield chunk
        finally:
            tmp.unlink(missing_ok=True)

    headers = {"Content-Disposition": f'attachment; filename="{safe_zip}.zip"'}
    return StreamingResponse(_stream(), media_type="application/zip", headers=headers)