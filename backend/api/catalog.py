"""分类树与关键词 API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import Category, CategoryKeyword, ClassifyRule, Document
from backend.schemas import CategoryIn, KeywordsIn

router = APIRouter(prefix="/api/categories", tags=["categories"])


def _node(c: Category, children_map: dict, doc_count: dict) -> dict:
    return {"id": c.id, "name": c.name, "path": c.path, "parent_id": c.parent_id,
            "color": c.color, "sort_order": c.sort_order,
            "doc_count": doc_count.get(c.id, 0),
            "children": [_node(k, children_map, doc_count) for k in children_map.get(c.id, [])]}


@router.get("/tree")
def tree(db: Session = Depends(get_db)):
    cats = db.query(Category).order_by(Category.sort_order, Category.id).all()
    children: dict[int | None, list] = {}
    for c in cats:
        children.setdefault(c.parent_id, []).append(c)
    from sqlalchemy import func

    counts = dict(db.query(Document.archived_category_id, func.count(Document.id))
                  .filter(Document.archived_category_id.isnot(None))
                  .group_by(Document.archived_category_id).all())
    return [_node(c, children, counts) for c in children.get(None, [])]


@router.get("/flat")
def flat(db: Session = Depends(get_db)):
    return [{"id": c.id, "name": c.name, "path": c.path, "parent_id": c.parent_id}
            for c in db.query(Category).order_by(Category.path).all()]


@router.post("")
def create(req: CategoryIn, db: Session = Depends(get_db)):
    path = req.name
    if req.parent_id:
        parent = db.get(Category, req.parent_id)
        if not parent:
            raise HTTPException(404, "父分类不存在")
        path = f"{parent.path}/{req.name}"
    if db.query(Category).filter(Category.path == path).first():
        raise HTTPException(400, "同名分类已存在")
    c = Category(name=req.name, path=path, parent_id=req.parent_id, color=req.color,
                 sort_order=db.query(Category).count())
    db.add(c)
    db.commit()
    return {"id": c.id, "path": path}


@router.patch("/{cat_id}")
def update(cat_id: int, req: CategoryIn, db: Session = Depends(get_db)):
    c = db.get(Category, cat_id)
    if not c:
        raise HTTPException(404, "不存在")
    c.name, c.color = req.name, req.color
    c.path = _rebuild_path(db, c)
    _cascade_paths(db, c)
    db.commit()
    return {"ok": True, "path": c.path}


def _rebuild_path(db: Session, c: Category) -> str:
    if not c.parent_id:
        return c.name
    parent = db.get(Category, c.parent_id)
    return f"{parent.path}/{c.name}" if parent else c.name


def _cascade_paths(db: Session, parent: Category) -> None:
    for child in db.query(Category).filter(Category.parent_id == parent.id).all():
        child.path = f"{parent.path}/{child.name}"
        _cascade_paths(db, child)


@router.delete("/{cat_id}")
def delete(cat_id: int, db: Session = Depends(get_db)):
    c = db.get(Category, cat_id)
    if not c:
        raise HTTPException(404, "不存在")
    if db.query(Category).filter(Category.parent_id == cat_id).count():
        raise HTTPException(400, "请先删除子分类")
    if db.query(Document).filter(Document.archived_category_id == cat_id).count():
        raise HTTPException(400, "该分类下已有归档文档，不能删除")
    db.query(ClassifyRule).filter(ClassifyRule.category_id == cat_id).update({"category_id": None})
    db.delete(c)
    db.commit()
    return {"ok": True}


@router.get("/{cat_id}/keywords")
def get_keywords(cat_id: int, db: Session = Depends(get_db)):
    return [k.keyword for k in db.query(CategoryKeyword).filter(
        CategoryKeyword.category_id == cat_id).all()]


@router.put("/{cat_id}/keywords")
def put_keywords(cat_id: int, req: KeywordsIn, db: Session = Depends(get_db)):
    if not db.get(Category, cat_id):
        raise HTTPException(404, "不存在")
    db.query(CategoryKeyword).filter(CategoryKeyword.category_id == cat_id).delete()
    for kw in req.keywords[:100]:
        if kw.strip():
            db.add(CategoryKeyword(category_id=cat_id, keyword=kw.strip()[:100]))
    db.commit()
    return {"ok": True}