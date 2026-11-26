"""对账 API：台账管理 / 对账运行 / 差异处理 / 导出 / 巡检状态。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import get_db
from backend.models import Category, Ledger, ReconDiff, ReconRun
from backend.schemas import ReconRunRequest, ResolveRequest
from backend.services import reconciliation as svc
from backend.services.pipeline import pipeline
from backend.services.reconciliation import sync_scheduler
from backend.services.uploads import stage_uploads
from backend.utils.fs import safe_filename, unique_filename
from backend.utils.logger import get_logger

router = APIRouter(prefix="/api/reconciliation", tags=["reconciliation"])
logger = get_logger("api.recon")


@router.post("/ledgers")
async def upload_ledger(name: str = Form(...), file: UploadFile = File(...),
                        db: Session = Depends(get_db)):
    if not (file.filename or "").lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "仅支持 .xlsx 台账")
    target = unique_filename(settings.ledger_root, safe_filename(name, ".xlsx"))
    with open(target, "wb") as out:
        while chunk := await file.read(1 << 20):
            out.write(chunk)
    try:
        parsed = svc.read_ledger_file(target)
    except Exception as e:  # noqa: BLE001
        target.unlink(missing_ok=True)
        raise HTTPException(400, f"台账解析失败: {e}") from e
    if not parsed["contract"]:
        target.unlink(missing_ok=True)
        raise HTTPException(400, "未识别到合同台账工作表（需含合同编号/金额等表头）")
    ledger = svc.save_ledger(db, name, str(target), parsed)
    return {"id": ledger.id, "rows": len(ledger.rows), "sheets": parsed["sheets"],
            "detected_contract_rows": len(parsed["contract"]),
            "settlement_rows": len(parsed["settlement"]), "payment_rows": len(parsed["payment"])}


@router.get("/ledgers")
def ledgers(db: Session = Depends(get_db)):
    return [{"id": l.id, "name": l.name, "rows": len(l.rows),
             "created_at": l.created_at.isoformat(timespec="seconds")} for l in db.query(Ledger).all()]


@router.delete("/ledgers/{ledger_id}")
def delete_ledger(ledger_id: int, db: Session = Depends(get_db)):
    l = db.get(Ledger, ledger_id)
    if not l:
        raise HTTPException(404, "不存在")
    p = settings.ledger_root / l.name
    try:
        from backend.services.archive import os_remove
        from pathlib import Path
        import os

        fp = Path(l.path)
        if fp.is_file():
            os_remove(fp)
    except OSError:
        pass
    db.delete(l)
    db.commit()
    return {"ok": True}


@router.post("/runs")
def run(req: ReconRunRequest, db: Session = Depends(get_db)):
    """source=auto 有台账用台账、没台账自动转档案库自对账。"""
    try:
        r = svc.run_reconciliation(db, req.ledger_id, req.source)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"run_id": r.id, "counts": r.counts, "total_rows": r.total_rows,
            "total_docs": r.total_docs, "source": r.source}


@router.get("/runs")
def runs(limit: int = 20, db: Session = Depends(get_db)):
    return [{"id": r.id, "status": r.status, "counts": r.counts, "source": r.source,
             "total_rows": r.total_rows, "total_docs": r.total_docs,
             "started_at": r.started_at.isoformat(timespec="seconds"),
             "finished_at": r.finished_at.isoformat(timespec="seconds") if r.finished_at else None}
            for r in db.query(ReconRun).order_by(ReconRun.id.desc()).limit(limit).all()]


@router.delete("/runs/{run_id}")
def delete_run(run_id: int, db: Session = Depends(get_db)):
    """删期：测试/无效的对账结果能清掉，diffs 随 cascade 一起消失。"""
    run = db.get(ReconRun, run_id)
    if not run:
        raise HTTPException(404, "运行不存在")
    db.delete(run)
    db.commit()
    return {"ok": True, "id": run_id}


def _diff_out(d: ReconDiff) -> dict:
    return {"id": d.id, "key": d.key, "type": d.diff_type, "label": svc.DIFF_TYPE_LABEL.get(d.diff_type, d.diff_type),
            "severity": d.severity, "contract_no": d.contract_no, "customer": d.customer,
            "document_id": d.document_id, "expected": d.expected, "actual": d.actual,
            "detail": d.detail, "resolved": d.resolved, "resolution_note": d.resolution_note}


@router.get("/runs/{run_id}/diffs")
def diffs(run_id: int, only_open: bool = False, type: str | None = None,
          db: Session = Depends(get_db)):
    if not db.get(ReconRun, run_id):
        raise HTTPException(404, "运行不存在")
    return [_diff_out(d) for d in svc.diffs_query(db, run_id, only_open, type)]


@router.post("/diffs/{diff_id}/resolve")
def resolve(diff_id: int, req: ResolveRequest, db: Session = Depends(get_db)):
    d = db.get(ReconDiff, diff_id)
    if not d:
        raise HTTPException(404, "差异不存在")
    svc.resolve_diff(db, d, req.note)
    run = svc.run_of(db, d)
    return {"ok": True, "counts": run.counts if run else None}


@router.get("/diffs/{diff_id}/evidence")
def evidence(diff_id: int, db: Session = Depends(get_db)):
    """差异的依据：所属业务包 + 已匹配文档 + 仍缺项（缺什么就给什么上传入口）。"""
    d = db.get(ReconDiff, diff_id)
    if not d:
        raise HTTPException(404, "差异不存在")
    return svc.diff_evidence(db, d)


@router.post("/diffs/{diff_id}/upload-doc")
async def upload_doc_for_diff(diff_id: int, files: list[UploadFile] = File(...),
                              db: Session = Depends(get_db)):
    """从差异直接补单据：落盘→整理流水线→（缺件角色作为分类提示）→前端轮询 job→recheck。"""
    d = db.get(ReconDiff, diff_id)
    if not d:
        raise HTTPException(404, "差异不存在")
    if d.resolved:
        raise HTTPException(400, "该差异已关闭")
    saved, rejected = await stage_uploads(files)
    if not saved:
        return {"diff_id": diff_id, "job_id": None, "saved": [], "rejected": rejected,
                "hint_role": ""}
    role = (d.detail or {}).get("required_role") or ""
    if not role and d.diff_type in svc.PAYMENT_DIFF_TYPES:
        role = "银行回单"          # 回款差异的占位类型：让流水线把上传件直接归到财务/银行回单
    hints: dict[str, str] = {}
    if role:
        cat = db.query(Category).filter(Category.name == role).first()
        if cat:
            hints = {str(p): cat.name for p in saved}
    job = pipeline.start_files_job(db, saved, kind="upload", source="recon_gap", hints=hints)
    return {"diff_id": diff_id, "job_id": job.id, "saved": [p.name for p in saved],
            "rejected": rejected, "hint_role": role if hints else "",
            "job": {"id": job.id, "total": job.total}}


@router.post("/diffs/{diff_id}/recheck")
def recheck(diff_id: int, db: Session = Depends(get_db)):
    """补齐后复核：满足就自动关闭并留痕文档 id，不满足返回仍缺什么。"""
    d = db.get(ReconDiff, diff_id)
    if not d:
        raise HTTPException(404, "差异不存在")
    out = svc.recheck_diff(db, d)
    out["evidence"] = svc.diff_evidence(db, d)
    return out


@router.get("/runs/{run_id}/export")
def export(run_id: int, fmt: str = "csv", db: Session = Depends(get_db)):
    if not db.get(ReconRun, run_id):
        raise HTTPException(404, "运行不存在")
    if fmt == "xlsx":
        data = svc.export_diffs_xlsx(db, run_id)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        fname = f"对账差异_{run_id}.xlsx"
    else:
        data = svc.export_diffs_csv(db, run_id)
        media = "text/csv"
        fname = f"对账差异_{run_id}.csv"
    from urllib.parse import quote

    return Response(content=data, media_type=media,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"})


@router.post("/scheduler")
def scheduler(enabled: bool, db: Session = Depends(get_db)):
    settings.update(recon_enabled=enabled)
    running = sync_scheduler()
    return {"ok": True, "running": running}


@router.get("/scheduler/status")
def scheduler_status():
    return svc._status