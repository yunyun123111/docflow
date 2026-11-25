"""预览与缩略图渲染：PDF 走 PyMuPDF，图片走 Pillow，表格走 openpyxl/csv。

设计要点
- 全部本地渲染，零网络依赖；结果按 (文档 id, 文件 mtime, 尺寸) 落盘缓存，重复打开秒出图。
- 只读渲染：绝不改动原文件；缓存写在 temp/thumbs，可被自检「清理预览缓存」回收。
- 路径守卫：只允许渲染受管目录内的文件——归档/待整理/回收站/台账/样例/临时/备份，
  外加监控目录与历史整理任务记录过的源目录（这两类是「原地处理」，文件不会被搬进来）。
"""
from __future__ import annotations

import csv as _csv
import io
from pathlib import Path

from backend.config import settings
from backend.utils.fs import fs_path
from backend.utils.logger import get_logger

logger = get_logger("preview")

MAX_TABLE_ROWS = 300
MAX_TABLE_COLS = 40
MAX_TEXT_CHARS = 60_000
MIN_PIXELS = 1_200_000       # 超过该像素数即降采样，避免大图预览卡顿
MAX_PAGES_META = 5_000
CACHE_MAX_FILES = 4000
CACHE_KEEP_FILES = 2000


class PreviewError(Exception):
    """无法预览（文件缺失、类型不支持、渲染失败）。"""


def _job_source_roots(db) -> list[Path]:
    """历史整理任务记录过的源目录。按目录整理/无人值守监控都是「原地处理」，
    文件不进收件目录，因此这些目录同样是用户显式授权过的受管范围，必须允许预览。"""
    from backend.models import ProcessingJob

    out: list[Path] = []
    try:
        rows = db.query(ProcessingJob.source).distinct().all()
    except Exception as exc:  # noqa: BLE001
        logger.info("读取整理任务目录失败：%s", exc)
        return out
    for (raw,) in rows:
        # 监控任务用 ";" 连接多个目录；上传/收件箱等不是路径，直接跳过。
        for part in str(raw or "").split(";"):
            part = part.strip().strip('"')
            try:
                cand = Path(part)
                if not cand.is_absolute():
                    continue
                real = cand.resolve()
            except OSError:
                continue
            if real.is_dir():
                out.append(real)
    return out


def _allowed_roots(db=None) -> list[Path]:
    roots = [settings.document_root, settings.inbox_root, settings.recycle_root,
             settings.ledger_root, settings.sample_root, settings.temp_dir, settings.backup_root]
    roots += list(settings.watch_dirs)
    if db is not None:
        roots += _job_source_roots(db)
    return roots


def resolve_path(raw: str, db=None) -> Path:
    """把数据库里的路径解析成真实路径，并确认它落在受管目录内（防越界读取）。"""
    if not raw:
        raise PreviewError("记录中没有文件路径")
    try:
        real = Path(str(raw)).resolve()
    except OSError as exc:
        raise PreviewError(f"路径无法解析：{str(exc)[:120]}") from exc
    if not real.is_file():
        raise PreviewError("磁盘上找不到该文件（可能已被移动或正在恢复）")
    roots = [r.resolve() for r in _allowed_roots(db) if r.is_dir()]
    if not any(real == r or r in real.parents for r in roots):
        raise PreviewError("文件不在受管目录内，出于安全不做预览")
    return real


def thumb_dir() -> Path:
    d = settings.temp_dir / "thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_file(doc_id: int, mtime: int, tag: str) -> Path:
    return thumb_dir() / f"d{doc_id}_{mtime}_{tag}.png"


def _read_cache(f: Path) -> bytes | None:
    try:
        return f.read_bytes() if f.is_file() else None
    except OSError:
        return None


def _write_cache(f: Path, data: bytes) -> None:
    try:
        f.write_bytes(data)
        _rotate_cache()
    except OSError:
        pass


def _rotate_cache() -> None:
    """缓存目录防爆：文件数超上限时按 mtime 淘汰最旧的一批。"""
    files = sorted(thumb_dir().glob("*.png"), key=lambda x: x.stat().st_mtime)
    if len(files) <= CACHE_MAX_FILES:
        return
    for f in files[: len(files) - CACHE_KEEP_FILES]:
        try:
            f.unlink()
        except OSError:
            pass


def clear_cache() -> int:
    """清空缩略图/页面缓存，返回删除的文件数。"""
    d = settings.temp_dir / "thumbs"
    n = 0
    if d.is_dir():
        for f in d.glob("*.png"):
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
    return n


def pdf_page_count(path: Path) -> int:
    try:
        import fitz

        with fitz.open(fs_path(path)) as doc:
            return min(int(doc.page_count), MAX_PAGES_META)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 PDF 页数失败（%s）：%s", path.name, exc)
        return 0


def render_pdf_page(path: Path, index: int, *, zoom: float = 1.5, max_width: int = 0) -> bytes:
    """渲染 PDF 某一页为 PNG。zoom 1.5 足够清晰，max_width 用于缩略图。"""
    import fitz

    with fitz.open(fs_path(path)) as doc:
        if not doc.page_count:
            raise PreviewError("PDF 没有可渲染的页面")
        index = max(0, min(int(index), doc.page_count - 1))
        page = doc.load_page(index)
        if max_width:
            ratio = min(1.0, max_width / (page.rect.width * zoom or 1))
            zoom = max(0.2, zoom * ratio)
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return pix.tobytes("png")


def render_image(path: Path, *, max_side: int = 1600, quality: int = 84) -> bytes:
    """图片缩放并转存 JPEG（自动纠正 EXIF 方向、CMYK/调色板转 RGB）。"""
    from PIL import Image, ImageOps

    with Image.open(fs_path(path)) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode == "P":
            im = im.convert("RGBA")
        if im.mode in ("RGBA", "PA", "LA"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        w, h = im.size
        ratio = min(1.0, max_side / max(w, h))
        if w * h > MIN_PIXELS and ratio >= 1.0:
            ratio = min(1.0, (MIN_PIXELS / (w * h)) ** 0.5)
        if ratio < 1.0:
            im = im.resize((max(1, int(w * ratio)), max(1, int(h * ratio))), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=quality, optimize=True)
        return out.getvalue()


def _table_from_text(text: str) -> dict:
    sample = text[:200_000]
    delim = max((",", "\t", ";"), key=lambda d: sample.count(d))
    rows: list[list[str]] = []
    try:
        for i, row in enumerate(_csv.reader(io.StringIO(sample), delimiter=delim)):
            rows.append([str(x)[:80] for x in row[:MAX_TABLE_COLS]])
            if i >= MAX_TABLE_ROWS:
                break
    except Exception as exc:  # noqa: BLE001
        raise PreviewError(f"CSV 解析失败：{str(exc)[:100]}") from exc
    return _pack_table(rows, truncated=len(text) > 200_000)


def _table_from_excel(path: Path) -> dict:
    from openpyxl import load_workbook

    wb = load_workbook(fs_path(path), read_only=True, data_only=True)
    try:
        names = [str(s.title) for s in wb.worksheets]
        rows: list[list[str]] = []
        for r in wb.worksheets[0].iter_rows(values_only=True):
            rows.append(["" if v is None else str(v)[:80] for v in list(r)[:MAX_TABLE_COLS]])
            if len(rows) > MAX_TABLE_ROWS:
                break
        out = _pack_table(rows, truncated=len(rows) >= MAX_TABLE_ROWS)
        out["sheets"] = names
        return out
    finally:
        wb.close()


def _pack_table(rows: list[list[str]], *, truncated: bool) -> dict:
    width = max((len(r) for r in rows[:5]), default=0)
    cols = [f"列{j + 1}" for j in range(width)]
    body = rows
    if rows:
        head = [str(x).strip() for x in rows[0]]
        if width and all(head) and len("".join(head)) <= 240:
            cols = head[:width]
            body = rows[1:]
    return {"kind": "table", "columns": cols, "rows": body[:MAX_TABLE_ROWS],
            "sheets": [], "truncated": truncated}


def table_preview(path: Path, file_type: str, fallback_text: str = "") -> dict:
    """表格类预览：优先解析原件（结构化单元格），失败时退回已入库文本。"""
    if file_type == "csv":
        return _table_from_text(path.read_text(encoding="utf-8", errors="replace"))
    if file_type in ("xlsx", "xlsm"):
        try:
            return _table_from_excel(path)
        except Exception as exc:  # noqa: BLE001
            logger.info("表格解析失败，退回文本预览：%s", exc)
    if fallback_text.strip():
        return {"kind": "table", "columns": [], "rows": [], "sheets": [],
                "text_body": fallback_text[:MAX_TEXT_CHARS],
                "truncated": len(fallback_text) > MAX_TEXT_CHARS}
    raise PreviewError("该表格暂时无法解析，可下载原件查看")


def thumbnail(doc_id: int, path: Path, file_type: str, size: int = 320) -> bytes | None:
    """列表/卡片缩略图：PDF 取首页，图片缩放；其余类型返回 None（前端显示类型徽标）。"""
    if file_type not in ("pdf", "image"):
        return None
    size = max(64, min(int(size), 640))
    cache = _cache_file(doc_id, int(path.stat().st_mtime), f"t{size}")
    hit = _read_cache(cache)
    if hit:
        return hit
    try:
        data = (render_pdf_page(path, 0, zoom=1.2, max_width=size) if file_type == "pdf"
                else render_image(path, max_side=size, quality=76))
    except Exception as exc:  # noqa: BLE001
        logger.info("缩略图生成失败 #%s：%s", doc_id, exc)
        return None
    _write_cache(cache, data)
    return data


def page_image(doc_id: int, path: Path, file_type: str, index: int, width: int) -> bytes:
    """按页渲染（预览翻页、审核页码条），带磁盘缓存。"""
    width = max(160, min(int(width) if width else 1000, 2400))
    if file_type == "pdf":
        cache = _cache_file(doc_id, int(path.stat().st_mtime), f"p{index}_w{width}")
        hit = _read_cache(cache)
        if hit:
            return hit
        zoom = 1.5 if width >= 900 else max(0.6, width / 800.0)
        data = render_pdf_page(path, index, zoom=zoom, max_width=width)
        _write_cache(cache, data)
        return data
    if file_type == "image":
        return render_image(path, max_side=width)
    raise PreviewError("该类型没有分页预览")