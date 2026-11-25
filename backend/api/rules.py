"""规则管理：识别规则 / 重命名模板 / 文件名规则 / 规则模拟器 / 学习建议。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import ClassifyRule, FilenameRule, RenameTemplate
from backend.schemas import (
    FilenameRuleIn, RenameTemplateIn, RuleIn, SimulateRequest,
)
from backend.services import rules as rule_service
from backend.services.learning import adopt, suggestions
from backend.services.pipeline import pipeline

router = APIRouter(prefix="/api/rules", tags=["rules"])


def _rule_out(r: ClassifyRule) -> dict:
    return {"id": r.id, "name": r.name, "pattern": r.pattern, "match_type": r.match_type,
            "field": r.field, "category_id": r.category_id, "document_type": r.document_type,
            "confidence": r.confidence, "priority": r.priority, "enabled": r.enabled}


@router.get("")
def list_rules(db: Session = Depends(get_db)):
    return [_rule_out(r) for r in db.query(ClassifyRule).order_by(ClassifyRule.priority, ClassifyRule.id).all()]


@router.post("")
def create_rule(req: RuleIn, db: Session = Depends(get_db)):
    r = ClassifyRule(**req.model_dump())
    db.add(r)
    db.commit()
    return _rule_out(r)


@router.put("/{rule_id}")
def update_rule(rule_id: int, req: RuleIn, db: Session = Depends(get_db)):
    r = db.get(ClassifyRule, rule_id)
    if not r:
        raise HTTPException(404, "规则不存在")
    for k, v in req.model_dump().items():
        setattr(r, k, v)
    db.commit()
    return _rule_out(r)


@router.delete("/{rule_id}")
def delete_rule(rule_id: int, db: Session = Depends(get_db)):
    r = db.get(ClassifyRule, rule_id)
    if not r:
        raise HTTPException(404, "规则不存在")
    db.delete(r)
    db.commit()
    return {"ok": True}


# ---------- 重命名模板 ----------
@router.get("/templates/rename")
def list_templates(db: Session = Depends(get_db)):
    return [{"id": t.id, "name": t.name, "pattern": t.pattern, "document_type": t.document_type,
             "is_default": t.is_default} for t in db.query(RenameTemplate).all()]


@router.post("/templates/rename")
def create_template(req: RenameTemplateIn, db: Session = Depends(get_db)):
    if req.is_default:
        db.query(RenameTemplate).filter(RenameTemplate.is_default.is_(True)).update({"is_default": False})
    t = RenameTemplate(**req.model_dump())
    db.add(t)
    db.commit()
    return {"id": t.id}


@router.put("/templates/rename/{tpl_id}")
def update_template(tpl_id: int, req: RenameTemplateIn, db: Session = Depends(get_db)):
    t = db.get(RenameTemplate, tpl_id)
    if not t:
        raise HTTPException(404, "模板不存在")
    if req.is_default:
        db.query(RenameTemplate).filter(RenameTemplate.is_default.is_(True)).update({"is_default": False})
    for k, v in req.model_dump().items():
        setattr(t, k, v)
    db.commit()
    return {"ok": True}


@router.delete("/templates/rename/{tpl_id}")
def delete_template(tpl_id: int, db: Session = Depends(get_db)):
    t = db.get(RenameTemplate, tpl_id)
    if not t:
        raise HTTPException(404, "模板不存在")
    db.delete(t)
    db.commit()
    return {"ok": True}


# ---------- 文件名规则 ----------
@router.get("/filename")
def list_filename_rules(db: Session = Depends(get_db)):
    return [{"id": r.id, "name": r.name, "regex": r.regex, "replacement": r.replacement,
             "priority": r.priority, "enabled": r.enabled}
            for r in db.query(FilenameRule).order_by(FilenameRule.priority).all()]


@router.post("/filename")
def create_filename_rule(req: FilenameRuleIn, db: Session = Depends(get_db)):
    import re

    try:
        re.compile(req.regex)
    except re.error as e:
        raise HTTPException(400, f"正则不合法: {e}") from e
    r = FilenameRule(**req.model_dump())
    db.add(r)
    db.commit()
    return {"id": r.id}


@router.put("/filename/{rule_id}")
def update_filename_rule(rule_id: int, req: FilenameRuleIn, db: Session = Depends(get_db)):
    r = db.get(FilenameRule, rule_id)
    if not r:
        raise HTTPException(404, "规则不存在")
    import re

    try:
        re.compile(req.regex)
    except re.error as e:
        raise HTTPException(400, f"正则不合法: {e}") from e
    for k, v in req.model_dump().items():
        setattr(r, k, v)
    db.commit()
    return {"ok": True}


@router.delete("/filename/{rule_id}")
def delete_filename_rule(rule_id: int, db: Session = Depends(get_db)):
    r = db.get(FilenameRule, rule_id)
    if not r:
        raise HTTPException(404, "规则不存在")
    db.delete(r)
    db.commit()
    return {"ok": True}


# ---------- 模拟器与学习 ----------
@router.post("/simulate")
def simulate(req: SimulateRequest, db: Session = Depends(get_db)):
    hits = rule_service.preview(db, req.filename, req.text)
    from backend.services.classifier import classify
    from backend.services.fields import extract

    ex = extract(req.filename, req.text)
    cls = classify(db, req.filename, req.text, ex.values)
    return {"rule_hits": hits,
            "extracted": {k: [v[0], v[1]] for k, v in ex.values.items()},
            "result": {"document_type": cls.document_type, "category_id": cls.category_id,
                       "category_path": cls.category_path, "confidence": cls.confidence,
                       "detail": cls.detail, "ai_used": cls.ai_used, "sample_used": cls.sample_used},
            "decision": ("auto_archive" if cls.confidence >= 0.85 else
                         "review" if cls.confidence >= 0.6 else "manual")}


@router.get("/learning/suggestions")
def learning_suggestions(db: Session = Depends(get_db)):
    return suggestions(db)


@router.post("/learning/adopt")
def learning_adopt(payload: dict, db: Session = Depends(get_db)):
    required = {"keyword", "category_id", "confidence"}
    if not required <= set(payload):
        raise HTTPException(400, "缺少字段: keyword/category_id/confidence")
    rule = adopt(db, {"keyword": payload["keyword"], "category_id": payload["category_id"],
                      "document_type": payload.get("document_type", ""),
                      "confidence": payload["confidence"]})
    return _rule_out(rule)