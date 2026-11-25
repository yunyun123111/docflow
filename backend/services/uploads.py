"""上传落盘共用逻辑：文档库上传与对账差异补单据共用同一套限流/校验。"""
from __future__ import annotations

from pathlib import Path

from fastapi import UploadFile

from backend.config import settings
from backend.services.parser import get_file_type
from backend.utils.fs import fs_path, safe_filename, unique_filename

CHUNK = 1 << 20


async def stage_uploads(files: list[UploadFile], dest_dir: Path | None = None,
                        max_mb: int | None = None) -> tuple[list[Path], list[dict]]:
    """流式写盘 + 大小上限 + 类型校验；返回 (已落盘路径, 被拒原因)。"""
    root = Path(dest_dir or settings.inbox_root)
    root.mkdir(parents=True, exist_ok=True)
    limit = (max_mb if max_mb is not None else settings.MAX_UPLOAD_MB) * 1024 * 1024
    saved: list[Path] = []
    rejected: list[dict] = []
    for f in files:
        name = Path(f.filename or "上传文件")
        target = unique_filename(root, safe_filename(name.stem, name.suffix))
        size = 0
        overflow = False
        with open(fs_path(target), "wb") as out:
            while chunk := await f.read(CHUNK):
                size += len(chunk)
                if size > limit:
                    overflow = True
                    break
                out.write(chunk)
        await f.close()
        if overflow:
            target.unlink(missing_ok=True)
            rejected.append({"filename": name.name,
                             "reason": f"超过 {limit // (1024 * 1024)}MB 上限"})
            continue
        if get_file_type(target) == "unknown":
            target.unlink(missing_ok=True)
            rejected.append({"filename": name.name, "reason": "不支持的文件类型"})
            continue
        saved.append(target)
    return saved, rejected
