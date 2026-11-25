"""命名：重命名模板 + 归档路径模板。

占位符：{date} {company} {counterparty} {contract_no} {amount} {type} {category}
{year} {month} {day} {title} {orig} {underscore}；{date} 优先取文档字段日期。
字段缺失导致段为空时自动跳过该段，连续下划线折叠。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from backend.models import RenameTemplate
from backend.services.fields import FIELD_AMOUNT, FIELD_COMPANY, FIELD_CONTRACT_NO, FIELD_COUNTERPARTY, FIELD_DATE
from backend.utils.fs import safe_filename

_FIELD_KEYS = {
    "company": FIELD_COMPANY, "counterparty": FIELD_COUNTERPARTY,
    "contract_no": FIELD_CONTRACT_NO, "amount": FIELD_AMOUNT,
}


def _field_str(fields: dict[str, tuple[Any, float]], name: str) -> str:
    v = fields.get(name)
    return str(v[0]) if v else ""


def _title_value(original: str, contract_no: str) -> str:
    """{title} 去掉已经单独成段的合同号，也丢掉 .pdf-1 这类上传残留。

    扫描件的文件名往往就是合同号，原样拼进模板会得到
    20260512_HSYCXS（DD）-2026-0236_HSYCXS（DD）-2026-0236.pdf-1.pdf 这种名字。
    """
    t = original or ""
    if contract_no and contract_no in t:
        t = t.replace(contract_no, "")
    t = t.strip(" _.-（）()【】")
    if not t or re.fullmatch(r"(?:pdf|jpg|jpeg|png|gif|bmp|webp|tiff|docx?|xlsx?|pptx?|txt|csv|pdf-\d+|\d+)[- _]?\d*", t, re.I):
        return ""
    return t


def _segment_value(key: str, fields: dict, doc_type: str, category_path: str, now: datetime, original: str) -> str:
    if key in _FIELD_KEYS:
        return _field_str(fields, _FIELD_KEYS[key])
    if key == "date":
        d = _field_str(fields, FIELD_DATE)
        return (d or now.strftime("%Y-%m-%d")).replace("-", "")
    if key == "year":
        return now.strftime("%Y")
    if key == "month":
        return now.strftime("%m")
    if key == "day":
        return now.strftime("%d")
    if key == "type":
        return doc_type or ""
    if key == "category":
        return (category_path or "").split("/")[-1] if category_path else ""
    if key == "title":
        return _title_value(original, _field_str(fields, FIELD_CONTRACT_NO))
    if key == "orig":
        return original or ""
    if key == "underscore":
        return "_"
    return ""  # 未知占位符按空处理


def _render(pattern: str, fields: dict, doc_type: str, category_path: str,
            now: datetime, original: str) -> str:
    out: list[str] = []
    rest = pattern
    while "{" in rest:
        head, _, tail = rest.partition("{")
        out.append(head)
        seg, _, rest = tail.partition("}")
        out.append(_segment_value(seg.strip(), fields, doc_type, category_path, now, original))
    out.append(rest)
    return "".join(out)


def render_name(pattern: str, fields: dict, ext: str, doc_type: str = "", category_path: str = "",
                now: datetime | None = None, original: str = "") -> str:
    now = now or datetime.now()
    base = re.sub(r"_{2,}", "_", _render(pattern, fields, doc_type, category_path, now, original)).strip("_.- ")
    if not base:
        base = original or "文档"
    return safe_filename(base, ext)


def render_path(template: str, fields: dict, doc_type: str, category_path: str,
                 now: datetime | None = None, original: str = "") -> list[str]:
    """归档目录模板逐段渲染；渲染后为空的段跳过。"""
    now = now or datetime.now()
    segments: list[str] = []
    for raw in (template or "").split("/"):
        raw = raw.strip()
        if not raw:
            continue
        seg = re.sub(r"_{2,}", "_", _render(raw, fields, doc_type, category_path, now, original)).strip("_.- ")
        if seg:
            segments.append(seg)
    return segments


def template_for(db: Session, doc_type: str) -> RenameTemplate:
    t = db.query(RenameTemplate).filter(RenameTemplate.document_type == (doc_type or "")).first()
    if t:
        return t
    t = db.query(RenameTemplate).filter(RenameTemplate.is_default.is_(True)).first()
    return t or RenameTemplate(name="通用", pattern="{date}{underscore}{title}")