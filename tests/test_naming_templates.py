"""模板真源与渲染口径：设置页改了就生效，字面分隔符与 {underscore} 等价。

前端「实时示例」按同一套规则算，这些断言就是它对齐的后端基准。
"""
from __future__ import annotations

from datetime import datetime

from backend.config import settings
from backend.models import RenameTemplate
from backend.services.fields import FIELD_CONTRACT_NO, FIELD_DATE
from backend.services.naming import render_name, render_path, template_for

NOW = datetime(2026, 11, 26, 9, 0, 0)
FULL = {FIELD_DATE: ("2026-05-12", 0.9), FIELD_CONTRACT_NO: ("HXCG-2026-088", 0.9)}
NO_CONTRACT = {FIELD_DATE: ("2026-05-12", 0.9)}

LITERAL = "{date}_{contract_no}_{title}"
UNDERSCORE = "{date}{underscore}{contract_no}{underscore}{title}"


def test_literal_separator_equals_underscore():
    """字段齐全 / 缺合同号 / 缺标题：三种缺失形态下新旧写法必须一模一样。"""
    cases = [("full", FULL, "设备采购合同"),
             ("no_contract", NO_CONTRACT, "设备采购合同"),
             ("no_title", FULL, "HXCG-2026-088")]
    for label, fields, original in cases:
        a = render_name(LITERAL, fields, ".pdf", original=original, now=NOW)
        b = render_name(UNDERSCORE, fields, ".pdf", original=original, now=NOW)
        assert a == b, label


def test_render_name_drops_empty_segments():
    assert render_name(LITERAL, NO_CONTRACT, ".pdf", original="设备采购合同", now=NOW) \
        == "20260512_设备采购合同.pdf"
    assert render_name(LITERAL, FULL, ".pdf", original="HXCG-2026-088", now=NOW) \
        == "20260512_HXCG-2026-088.pdf"


def test_unknown_placeholder_renders_empty():
    """{index} 只在批量确认的 base_filename 里有效，模板里必须安静地变空。"""
    assert render_name("{index}{date}", FULL, ".pdf", now=NOW) == "20260512.pdf"
    assert render_name("{index}", {}, ".pdf", now=NOW) == "文档.pdf"


def test_template_for_uses_settings_as_generic_default(db):
    assert template_for(db, "").pattern == settings.RENAME_TEMPLATE
    assert template_for(db, "没这个类型").pattern == settings.RENAME_TEMPLATE


def test_template_for_prefers_per_type_row(db):
    row = RenameTemplate(name="测试回单", document_type="测试回单类型", pattern="HD_{date}")
    db.add(row)
    db.commit()
    try:
        assert template_for(db, "测试回单类型").pattern == "HD_{date}"
    finally:
        db.delete(row)
        db.commit()


def test_template_for_ignores_legacy_generic_row(db):
    """老库里的“通用”行不再参与取名，否则设置页又变成改了不生效的第二真源。"""
    row = RenameTemplate(name="旧通用", document_type="", pattern="{orig}", is_default=True)
    db.add(row)
    db.commit()
    try:
        assert template_for(db, "").pattern == settings.RENAME_TEMPLATE
        assert template_for(db, "没这个类型").pattern == settings.RENAME_TEMPLATE
    finally:
        db.delete(row)
        db.commit()


def test_render_path_skips_empty_and_unknown_segments():
    assert render_path("{company}/{year}", NO_CONTRACT, "", "合同/采购合同", NOW) == ["2026"]
    assert render_path("", NO_CONTRACT, "", "合同/采购合同", NOW) == []
    assert render_path("{category}/{year}/{month}", NO_CONTRACT, "合同", "合同/采购合同", NOW) \
        == ["采购合同", "2026", "11"]
