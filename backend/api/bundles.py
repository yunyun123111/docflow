"""文档归集 API：业务包列表/详情、手工关联、合并建议、必备单据规则、重建归集。

注意：字面路径（/suggestions、/requirements、/rebuild）必须先于 /{bundle_id} 声明，
否则会被 int 参数拦截成 422。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import get_db
from backend.models import Bundle, DocRequirement
from backend.schemas import (
    BundleCreateRequest, BundleItemPatchRequest, BundleItemsRequest,
    BundleMergeRequest, BundlePatchRequest, DocRequirementIn,
)
from backend.services import bundles as svc
from backend.services.archive import log_operation
from backend.services.bundles import BundleError
from backend.utils.logger import get_logger

router = APIRouter(prefix="/api/bundles", tags=["bundles"])
logger = get_logger("api.bundles")


@router.get("")
def list_bundles(q: str = "", state: str = Query("", pattern="^(|missing|complete|open|settled)$"),
                 limit: int = 200, offset: int = 0, db: Session = Depends(get_db)):
    return svc.list_bundles(db, q=q, state=state, limit=limit, offset=offset)


@router.post("")
def create_bundle(req: BundleCreateRequest, db: Session = Depends(get_db)):
    bundle = svc.create_bundle(db, req.name, req.note)
    log_operation(db, "bundle_create", detail=f"#{bundle.id} {bundle.name}")
    db.commit()
    return svc.get_bundle_detail(db, bundle.id)


@router.get("/requirements")
def get_requirements(db: Session = Depends(get_db)):
    rows = db.query(DocRequirement).order_by(DocRequirement.contract_role,
                                             DocRequirement.id).all()
    return [{"id": r.id, "contract_role": r.contract_role, "required_role": r.required_role,
             "min_count": r.min_count, "amount_check": bool(r.amount_check),
             "enabled": bool(r.enabled)} for r in rows]


@router.put("/requirements")
def put_requirements(rows: list[DocRequirementIn], db: Session = Depends(get_db)):
    """整表替换：来沢外的角色组合会被删除，因此前端必须提交完整清单。"""
    kept: set[tuple[str, str]] = set()
    for r in rows:
        crole = (r.contract_role or "").strip()[:40]
        rrole = (r.required_role or "").strip()[:40]
        if not crole or not rrole:
            continue
        obj = (db.query(DocRequirement).filter(DocRequirement.contract_role == crole,
               DocRequirement.required_role == rrole).first())
        if obj is None:
            obj = DocRequirement(contract_role=crole, required_role=rrole)
            db.add(obj)
        obj.min_count = max(0, int(r.min_count or 0))
        obj.amount_check = bool(r.amount_check)
        obj.enabled = bool(r.enabled)
        kept.add((crole, rrole))
    for obj in db.query(DocRequirement).all():
        if (obj.contract_role, obj.required_role) not in kept:
            db.delete(obj)
    log_operation(db, "bundle_requirements", detail=f"必备清单 {len(kept)} 条")
    db.commit()
    return get_requirements(db)


@router.get("/suggestions")
def merge_suggestions(db: Session = Depends(get_db)):
    items = svc.suggestions(db)
    return {"enabled": bool(settings.BUNDLE_MERGE_SUGGEST_ENABLED), "items": items,
            "tolerance": settings.BUNDLE_MERGE_AMOUNT_TOLERANCE,
            "window_days": settings.BUNDLE_MERGE_DATE_WINDOW_DAYS}


@router.post("/rebuild")
def rebuild(db: Session = Depends(get_db)):
    """历史数据回填：重建全部自动包，手工包保留。"""
    out = svc.rebuild_all(db)
    log_operation(db, "bundle_rebuild", detail=str(out))
    db.commit()
    logger.info("重建归集 %s", out)
    return out


@router.patch("/items/{item_id}")
def patch_item(item_id: int, req: BundleItemPatchRequest, db: Session = Depends(get_db)):
    try:
        return svc.set_item_role(db, item_id, req.role)
    except BundleError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/{bundle_id}")
def bundle_detail(bundle_id: int, db: Session = Depends(get_db)):
    try:
        return svc.get_bundle_detail(db, bundle_id)
    except BundleError as e:
        raise HTTPException(404, str(e)) from e


@router.patch("/{bundle_id}")
def patch_bundle(bundle_id: int, req: BundlePatchRequest, db: Session = Depends(get_db)):
    bundle = db.get(Bundle, bundle_id)
    if not bundle:
        raise HTTPException(404, "业务包不存在")
    if req.name is not None and req.name.strip():
        bundle.name = req.name.strip()[:200]
        # 改名之后这个名字归用户了：重算包名和「重建归集」都不许再覆盖它
        bundle.source = "manual"
    if req.note is not None:
        bundle.note = req.note[:2000]
    if req.status is not None:
        if req.status not in ("open", "settled"):
            raise HTTPException(400, "status 只能是 open / settled")
        bundle.status = req.status
    db.commit()
    return svc.get_bundle_detail(db, bundle_id)


@router.delete("/{bundle_id}")
def delete_bundle(bundle_id: int, db: Session = Depends(get_db)):
    """只解散包，不删文档。"""
    bundle = db.get(Bundle, bundle_id)
    if not bundle:
        raise HTTPException(404, "业务包不存在")
    name = bundle.name
    db.delete(bundle)
    log_operation(db, "bundle_delete", detail=f"#{bundle_id} {name}")
    db.commit()
    return {"ok": True, "id": bundle_id}


@router.post("/{bundle_id}/items")
def add_items(bundle_id: int, req: BundleItemsRequest, db: Session = Depends(get_db)):
    bundle = db.get(Bundle, bundle_id)
    if not bundle:
        raise HTTPException(404, "业务包不存在")
    try:
        out = svc.add_documents(db, bundle, req.document_ids, role=req.role)
    except BundleError as e:
        raise HTTPException(400, str(e)) from e
    return out


@router.delete("/{bundle_id}/items/{document_id}")
def remove_item(bundle_id: int, document_id: int, db: Session = Depends(get_db)):
    bundle = db.get(Bundle, bundle_id)
    if not bundle:
        raise HTTPException(404, "业务包不存在")
    svc.remove_document(db, bundle, document_id)
    return {"ok": True, "bundle_id": bundle_id, "document_id": document_id}


@router.post("/{bundle_id}/merge")
def merge(bundle_id: int, req: BundleMergeRequest, db: Session = Depends(get_db)):
    """把 bundle_id 解散并入 target_id（条目搬家 + 重算主合同号 + 删源包）。"""
    try:
        return svc.merge_bundles(db, bundle_id, req.target_id, req.note)
    except BundleError as e:
        raise HTTPException(400, str(e)) from e
