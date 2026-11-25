"""金额与当事方口径：单价不是总额，我方与对方不能同值。"""
from __future__ import annotations

from backend.config import settings
from backend.services import fields as F

# 真实矿石销售合同的取数段落：单价与总额分行写，标签和数字之间还夹着「(人民币)」
ORE = ("矿石销售合同\n乙方：山西宏达钢铁有限公司（买方）\n签约时间：2026年5月12日\n"
       "数量\n10000吨（+/-10%）\n合同车板现汇含税价\n799.05元/湿吨\n"
       "合同总金额(人民币)\n7990500.00元\n合同不含税总金额（人民币）\n7071238.94元\n"
       "税额（税率13%）（人民币）\n919261.06元\n")
ORE_NO_TOTAL = ORE.replace("合同总金额(人民币)\n7990500.00元\n", "")


def _v(ex, name):
    got = ex.values.get(name)
    return got[0] if got else ""


def test_labeled_total_beats_unit_price():
    """带标签的总额才是金额；「799.05元/湿吨」是单价，拿它当金额对账必报假差异。"""
    ex = F.extract("合同.txt", ORE)
    assert _v(ex, F.FIELD_AMOUNT) == "7990500.00", ex.values
    assert _v(ex, F.FIELD_UNIT_PRICE) == "799.05", ex.values


def test_total_derived_from_unit_times_quantity():
    """合同只写单价与数量时金额要能算出来，否则字段覆盖率掉下来会卡在人工审核。"""
    ex = F.extract("合同.txt", ORE_NO_TOTAL)
    assert _v(ex, F.FIELD_AMOUNT) == "7990500.00", ex.values


def test_ambiguous_quantity_is_not_guessed():
    """数量不唯一就不硬算：凑出来的总额会让对账报出假的「金额不符」。"""
    ex = F.extract("合同.txt", "销售合同\n单价 799.05元/湿吨\n本批 10000吨，另批 500吨\n")
    assert F.FIELD_AMOUNT not in ex.values, ex.values
    assert _v(ex, F.FIELD_UNIT_PRICE) == "799.05"


def test_parties_are_two_different_names():
    """从前第一个公司名会同时写进「公司名称」和「对方单位」，两个字段是同一个值。"""
    ex = F.extract("合同.txt", "销售合同\n甲方：华山云创供应链科技有限公司\n乙方：山西宏达钢铁有限公司\n")
    assert _v(ex, F.FIELD_COUNTERPARTY) == "华山云创供应链科技有限公司"
    assert _v(ex, F.FIELD_COMPANY) == "山西宏达钢铁有限公司"
    assert _v(ex, F.FIELD_COMPANY) != _v(ex, F.FIELD_COUNTERPARTY)


def test_single_party_leaves_our_entity_empty():
    """只认得一个当事方时宁可留空，也不要把我方主体写成对方。"""
    ex = F.extract("结算单.txt", "华山云创供应链科技有限公司销售结算单\nTo:\n山西宏达钢铁有限公司\n")
    assert _v(ex, F.FIELD_COUNTERPARTY) == "山西宏达钢铁有限公司"
    assert F.FIELD_COMPANY not in ex.values, ex.values


def test_our_company_setting_decides_who_is_us(monkeypatch):
    """填了本公司名称：括号与空格不参与比对，客户取文中不是本公司的另一方。"""
    monkeypatch.setattr(settings, "OUR_COMPANY", "华山云创供应链科技（山西）有限公司")
    ex = F.extract("合同.txt", "矿石销售合同\n乙方：山西宏达钢铁有限公司\n"
                   "甲方：华山云创供应链科技（山西）有限公司\n")
    assert _v(ex, F.FIELD_COMPANY) == "华山云创供应链科技（山西）有限公司", ex.values
    assert _v(ex, F.FIELD_COUNTERPARTY) == "山西宏达钢铁有限公司", ex.values


def test_our_company_not_minted_into_coverage(monkeypatch):
    """本公司名称没在文档里出现时不写字段：白送的字段覆盖率会让误判直接自动归档。"""
    monkeypatch.setattr(settings, "OUR_COMPANY", "别的公司有限公司")
    ex = F.extract("合同.txt", "矿石销售合同\n乙方：山西宏达钢铁有限公司\n")
    assert F.FIELD_COMPANY not in ex.values, ex.values
    assert _v(ex, F.FIELD_COUNTERPARTY) == "山西宏达钢铁有限公司"

def test_label_must_have_a_separator():
    """「甲方收到供应商（…）货权转移单后」不是签署方；标签后面没冒号/空格就不算当事方。"""
    body = ("矿石销售合同\n甲方收到供应商（天柱深(北京)国际贸易有限公司）货权转移单后三个工作日内付款。\n"
            "乙方：山西宏达钢铁有限公司\n")
    ex = F.extract("合同.txt", body)
    assert _v(ex, F.FIELD_COUNTERPARTY) == "山西宏达钢铁有限公司", ex.values
    assert F.FIELD_COMPANY not in ex.values, ex.values


def test_entity_suffix_must_be_a_real_company_word():
    """「…为主张权利所」结尾的「所」不是公司名，不能当成当事方。"""
    ex = F.extract("合同.txt", "合同\n甲方：为主张权利所发生的费用由乙方承担\n乙方：山西宏达钢铁有限公司\n")
    assert _v(ex, F.FIELD_COUNTERPARTY) == "山西宏达钢铁有限公司", ex.values
    assert F.FIELD_COMPANY not in ex.values, ex.values


def test_to_from_layout_is_parsed():
    """结算单的 To:/From: 抬头要能认出客户与本公司。"""
    body = ("华山云创供应链科技（山西）有限公司销售结算单\nTo:\n山西宏达钢铁有限公司\nDate:\n"
            "2026/6/25\nFrom:\n华山云创供应链科技（山西）有限公司\n")
    ex = F.extract("结算单.jpg", body)
    assert _v(ex, F.FIELD_COUNTERPARTY) == "山西宏达钢铁有限公司", ex.values
    assert _v(ex, F.FIELD_COMPANY) == "华山云创供应链科技（山西）有限公司", ex.values
