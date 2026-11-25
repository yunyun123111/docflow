"""搜索：FTS5 全文（带片段高亮）+ 文件名/字段/类型 LIKE + 拼音兜底。

旧版只搜文件名与字段；新版正文可搜是核心升级。CJK 用 unicode61 单字切分，
查询词用短语（"xx"）保证相邻命中。
"""
from __future__ import annotations

import re

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from backend.models import Document, DocumentField
from backend.services.text_norm import normalize_for_match


def _fts_query(q: str) -> str:
    """查询词空格化后按短语匹配（与索引侧 space_cjk 对应）。"""
    from backend.models import space_cjk

    tokens = [t for t in re.split(r"\s+", q.strip()) if t]
    return " AND ".join('"' + space_cjk(t).replace('"', '""') + '"' for t in tokens)


def fts_search(db: Session, q: str, limit: int = 200) -> dict[int, str]:
    """返回 {doc_id: 命中片段}，按 bm25 排序。"""
    query = _fts_query(q)
    if not query:
        return {}
    from backend.models import unspace_cjk

    rows = db.execute(
        sql_text("""
            SELECT rowid, snippet(documents_fts, 0, '【', '】', ' ', 32) AS snip
            FROM documents_fts WHERE documents_fts MATCH :q
            ORDER BY bm25(documents_fts) LIMIT :lim
        """),
        {"q": query, "lim": limit},
    ).fetchall()
    return {int(r[0]): unspace_cjk(r[1] or "") for r in rows}


def like_ids(db: Session, q: str) -> set[int]:
    like = f"%{q}%"
    ids: set[int] = set()
    ids.update(r[0] for r in db.query(Document.id).filter(
        Document.original_filename.like(like) | Document.current_filename.like(like)
        | Document.title.like(like) | Document.document_type.like(like)).all())
    ids.update(r[0] for r in db.query(DocumentField.document_id).filter(DocumentField.value.like(like)).all())
    return ids


def pinyin_ids(db: Session, q: str) -> set[int]:
    """首字母/全拼 搜索文档类型（如 'ht' → 合同）。"""
    if not q.isascii() or len(q) < 2:
        return set()
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        return set()
    names = [r[0] for r in db.query(Document.document_type).distinct().all() if r[0]]
    hit_types: set[str] = set()
    for name in names:
        full = "".join(lazy_pinyin(name)).lower()
        init = "".join(lazy_pinyin(name, style=Style.FIRST_LETTER)).lower()
        if q.lower() in full or q.lower() in init:
            hit_types.add(name)
    if not hit_types:
        return set()
    return {r[0] for r in db.query(Document.id).filter(Document.document_type.in_(hit_types)).all()}


def search_ids(db: Session, q: str) -> tuple[list[int], dict[int, str]]:
    """综合搜索：FTS 命中优先（有序），其后文件名/字段命中按更新时间。"""
    q = (q or "").strip()
    if not q:
        return [], {}
    fts = fts_search(db, q)
    extra = (like_ids(db, q) | pinyin_ids(db, q)) - set(fts)
    ordered_fts = list(fts)
    if extra:
        rows = db.query(Document.id).filter(Document.id.in_(extra)).order_by(Document.created_at.desc()).all()
        ordered_fts.extend(r[0] for r in rows)
    return ordered_fts, fts


def fuzzy_keyword_on_text(db: Session, q: str, limit: int = 200) -> dict[int, str]:
    """FTS5 不可用环境的降级路径：normalize 后子串扫正文（小库可用）。"""
    out: dict[int, str] = {}
    nk = normalize_for_match(q)
    if not nk:
        return out
    from backend.models import DocumentText

    rows = db.query(DocumentText.document_id, DocumentText.text).limit(5000).all()
    for doc_id, body in rows:
        n = normalize_for_match(body or "")
        idx = n.find(nk)
        if idx >= 0:
            out[doc_id] = (body or "")[max(0, idx - 30): idx + len(q) + 30]
            if len(out) >= limit:
                break
    return out