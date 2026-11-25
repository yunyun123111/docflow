"""备份 API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import get_db
from backend.models import Ledger
from backend.schemas import BackupCreateRequest
from backend.services import backup as svc
from backend.services.backup import BackupError

router = APIRouter(prefix="/api/backup", tags=["backup"])


@router.get("/list")
def listing():
    return svc.list_backups()


@router.post("/create")
def create(req: BackupCreateRequest):
    try:
        return svc.create_backup(include_documents=req.include_documents)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"备份失败: {e}") from e


@router.get("/download/{filename}")
def download(filename: str):
    try:
        path = svc.get_backup_path(filename)
    except BackupError as e:
        raise HTTPException(400, str(e)) from e
    return FileResponse(str(path), filename=path.name, media_type="application/zip")


@router.delete("/{filename}")
def remove(filename: str):
    try:
        svc.delete_backup(filename)
    except BackupError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


@router.post("/restore")
async def restore(file: UploadFile = File(...), include_documents: bool = True,
                  db: Session = Depends(get_db)):
    """上传备份 zip 恢复；成员路径全部经过守卫校验。"""
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(400, "请上传 .zip 备份包")
    tmp = settings.temp_dir / f"restore_{file.filename}"
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    limit = settings.MAX_UPLOAD_MB * 1024 * 1024
    size = 0
    with open(tmp, "wb") as out:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > limit:
                tmp.unlink(missing_ok=True)
                raise HTTPException(413, "备份包过大")
            out.write(chunk)
    try:
        result = svc.restore_backup(tmp, include_documents=include_documents)
    except BackupError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"恢复失败: {e}") from e
    finally:
        tmp.unlink(missing_ok=True)
    return result