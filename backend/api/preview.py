"""预览 API：文档库与人工审核共用的预览数据、缩略图、逐页渲染。

与 documents.py 的分工：documents 管业务记录（列表/修正/归档），preview 只管「看内容」，
覆盖 PDF 逐页位图、图片、表格（csv/xlsx）、文本，以及台账 Excel 预览。
所有渲染都是本地计算，且受 services/preview.py 的目录守卫约束。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import Document, DocumentText, Ledger
from backend.services import preview as pv
from backend.services.preview import PreviewError
from backend.utils.fs import fs_path

router = APIRouter(prefix="/api/preview", tags=["preview"])

MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    "xls": "application/vnd.ms-excel",
    "csv": "text/csv",
    "txt": "text/plain; charset=utf-8",
}
TEXT_TYPES = ("docx", "doc", "txt", "md")
TABLE_TYPES = ("csv", "xlsx", "xlsm", "xls")


IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
              ".tif": "image/tiff", ".tiff": "image/tiff"}


def _media_type(doc: Document, path: Path | None = None) -> str:
    if doc.file_type == "image":
        suffix = (path.suffix if path else Path(doc.current_filename or doc.original_filename).suffix).lower()
        return IMAGE_MIME.get(suffix, "application/octet-stream")
    return MIME.get(doc.file_type, "application/octet-stream")


def _kind_of(file_type: str) -> str:
    if file_type == "pdf":
        return "pdf"
    if file_type == "image":
        return "image"
    if file_type in TABLE_TYPES:
        return "table"
    if file_type in TEXT_TYPES:
        return "text"
    return "binary"


def _doc_or_404(doc_id: int, db: Session) -> Document:
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    return doc


def _body_text(db: Session, doc_id: int) -> str:
    row = db.get(DocumentText, doc_id)
    return (row.text or "") if row else ""


@router.get("/{doc_id}")
def preview_meta(doc_id: int, db: Session = Depends(get_db)):
    """预览总览：前端据此决定用 pdf.js、图片、表格还是文本渲染。"""
    doc = _doc_or_404(doc_id, db)
    raw = doc.current_path or doc.original_path
    name = doc.current_filename or doc.original_filename
    kind = _kind_of(doc.file_type)
    out: dict = {
        "id": doc.id, "name": name, "file_type": doc.file_type, "kind": kind,
        "mime": _media_type(doc),
        "size": doc.file_size, "status": doc.status, "document_type": doc.document_type,
        "confidence": doc.confidence, "pages": 0, "exists": False,
        "file_url": f"/api/preview/{doc_id}/file",
        "thumb_url": f"/api/preview/{doc_id}/thumbnail",
        "page_url": f"/api/preview/{doc_id}/page/",
        "table": None, "text": None, "reason": "",
    }
    try:
        path = pv.resolve_path(raw, db)
        out["exists"] = True
    except PreviewError as exc:
        out["reason"] = str(exc)
        return out
    if kind == "pdf":
        out["pages"] = pv.pdf_page_count(path)
    elif kind == "table":
        try:
            out["table"] = pv.table_preview(path, doc.file_type, _body_text(db, doc_id))
        except PreviewError as exc:
            out["reason"] = str(exc)
            out["kind"] = "text" if _body_text(db, doc_id) else "binary"
    elif kind in ("text", "binary"):
        body = _body_text(db, doc_id)
        if body:
            out["kind"] = "text"
            out["text"] = {"body": body[: pv.MAX_TEXT_CHARS], "total_length": len(body),
                           "truncated": len(body) > pv.MAX_TEXT_CHARS}
        elif kind == "binary":
            out["reason"] = "该类型没有可显示的正文，请下载原件"
    return out


@router.get("/{doc_id}/file")
def preview_file(doc_id: int, db: Session = Depends(get_db), download: bool = False):
    """内联下发原件（带 Range 支持，pdf.js 可分段加载大文件）。"""
    doc = _doc_or_404(doc_id, db)
    try:
        path = pv.resolve_path(doc.current_path or doc.original_path, db)
    except PreviewError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(fs_path(path), media_type=_media_type(doc, path),
                        filename=doc.current_filename or doc.original_filename,
                        content_disposition_type="attachment" if download else "inline")


@router.get("/{doc_id}/thumbnail")
def preview_thumbnail(doc_id: int, size: int = Query(320, ge=64, le=640),
                      db: Session = Depends(get_db)):
    """列表/卡片缩略图；不支持的类型返回 404，前端改用类型徽标。"""
    doc = _doc_or_404(doc_id, db)
    try:
        path = pv.resolve_path(doc.current_path or doc.original_path, db)
        data = pv.thumbnail(doc_id, path, doc.file_type, size)
    except PreviewError as exc:
        raise HTTPException(404, str(exc)) from exc
    if not data:
        raise HTTPException(404, "该类型不提供缩略图")
    return _png(data, f"t-{doc_id}-{size}")


@router.get("/{doc_id}/page/{index}")
def preview_page(doc_id: int, index: int, width: int = Query(1000, ge=160, le=2400),
                 db: Session = Depends(get_db)):
    """逐页位图：审核时翻页、预览降级（pdf.js 不可用）与页码条都用它。"""
    doc = _doc_or_404(doc_id, db)
    try:
        path = pv.resolve_path(doc.current_path or doc.original_path, db)
        data = pv.page_image(doc_id, path, doc.file_type, index, width)
    except PreviewError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _png(data, f"p-{doc_id}-{index}-{width}")


@router.get("/{doc_id}/text")
def preview_text(doc_id: int, start: int = Query(0, ge=0), length: int = Query(20000, ge=200, le=60000),
                 db: Session = Depends(get_db)):
    """正文分页读取（长文档不一次性下发，避免卡顿）。"""
    doc = _doc_or_404(doc_id, db)
    body = _body_text(db, doc_id)
    end = min(len(body), start + length)
    return {"id": doc_id, "start": start, "end": end, "total_length": len(body),
            "body": body[start:end], "has_more": end < len(body)}


@router.get("/ledger/{ledger_id}")
def preview_ledger(ledger_id: int, db: Session = Depends(get_db)):
    """台账 Excel 预览（对账页直接看原始表）。"""
    led = db.get(Ledger, ledger_id)
    if not led:
        raise HTTPException(404, "台账不存在")
    try:
        path = pv.resolve_path(led.path, db)
        data = pv.table_preview(path, path.suffix.lstrip(".").lower(), "")
    except PreviewError as exc:
        raise HTTPException(400, str(exc)) from exc
    data["name"] = led.name
    data["sheets"] = data.get("sheets") or []
    return data


@router.post("/cache/clear")
def clear_preview_cache():
    return {"ok": True, "removed": pv.clear_cache()}


def _png(data: bytes, tag: str) -> Response:
    return Response(content=data, media_type="image/png", headers={
        "ETag": f'W/"{tag}"', "Cache-Control": "private, max-age=86400"})