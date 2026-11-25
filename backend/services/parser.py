"""统一解析接口：按扩展名分派；扫描件/图片自动走 OCR。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

EXT_TYPES = {
    ".pdf": "pdf", ".jpg": "image", ".jpeg": "image", ".png": "image",
    ".docx": "docx", ".doc": "doc", ".xlsx": "xlsx", ".xls": "xls",
    ".txt": "txt", ".csv": "csv",
}
MIME = {
    "pdf": "application/pdf", "image": "image/jpeg", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel", "txt": "text/plain", "csv": "text/csv", "unknown": "application/octet-stream",
}


def get_file_type(path: Path) -> str:
    return EXT_TYPES.get(path.suffix.lower(), "unknown")


@dataclass
class ParseResult:
    text: str = ""
    pages: int = 0
    ocr_used: bool = False
    warnings: list[str] = field(default_factory=list)


def parse_file(path: Path) -> ParseResult:
    kind = get_file_type(path)
    if kind == "pdf":
        return _parse_pdf(path)
    if kind in ("docx", "doc"):
        return _parse_word(path)
    if kind in ("xlsx", "xls"):
        return _parse_excel(path)
    if kind in ("txt", "csv"):
        return _parse_text(path)
    if kind == "image":
        return _parse_image(path)
    raise ValueError(f"不支持的文件类型: {path.suffix}")


def _parse_text(path: Path) -> ParseResult:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return ParseResult(text=raw.decode(enc), pages=1)
        except UnicodeDecodeError:
            continue
    return ParseResult(text=raw.decode("utf-8", "ignore"), pages=1, warnings=["编码无法识别，已按 utf-8 忽略错误解码"])


def _parse_pdf(path: Path) -> ParseResult:
    import fitz

    out: list[str] = []
    ocr_pages = 0
    with fitz.open(str(path)) as doc:
        for page in doc:
            t = page.get_text("text").strip()
            if len(t) < 20 and OCR_AVAILABLE():
                r = ocr_page_image(page)
                if r:
                    out.append(r)
                    ocr_pages += 1
                    continue
            out.append(t)
        pages = doc.page_count
    res = ParseResult(text="\n".join(out), pages=pages, ocr_used=ocr_pages > 0)
    if ocr_pages:
        res.warnings.append(f"{ocr_pages}/{pages} 页由 OCR 识别")
    return res


def _parse_word(path: Path) -> ParseResult:
    import docx

    d = docx.Document(str(path))
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            parts.append("\t".join(c.text.strip() for c in row.cells))
    return ParseResult(text="\n".join(parts), pages=1)


def _parse_excel(path: Path) -> ParseResult:
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    parts: list[str] = []
    for ws in wb.worksheets:
        parts.append(f"# {ws.title}")
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                parts.append("\t".join(cells))
    wb.close()
    return ParseResult(text="\n".join(parts), pages=len(wb.sheetnames) if hasattr(wb, "sheetnames") else 1)


def _parse_image(path: Path) -> ParseResult:
    if not OCR_AVAILABLE():
        raise ValueError("图片文件需要 OCR，但 OCR 未启用或依赖缺失")
    from backend.services.ocr import ocr_image

    text = ocr_image(path)
    return ParseResult(text=text, pages=1, ocr_used=True)


def OCR_AVAILABLE() -> bool:
    from backend.config import settings
    return settings.OCR_ENABLED


def ocr_page_image(page) -> str:
    from backend.services.ocr import ocr_pixmap
    import fitz

    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
    return ocr_pixmap(pix)