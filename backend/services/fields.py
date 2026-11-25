"""字段提取：公司名称 / 日期 / 编号 / 金额（纯规则、零 token）。"""
from __future__ import annotations

import re
from dataclasses import dataclass

from backend.config import settings

FIELD_COMPANY = "公司名称"
FIELD_CONTRACT_NO = "合同编号"
FIELD_DATE = "日期"
FIELD_AMOUNT = "金额"
FIELD_COUNTERPARTY = "对方单位"
FIELD_UNIT_PRICE = "单价"


@dataclass
class Extracted:
    values: dict[str, tuple[str, float]]


_DATE_RE = re.compile(r"(20\d{2})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})")
# 显式标签优先：发票上“发票号码/开票日期”很常见，靠位置取胜会把发票号当成合同号，
# 归集与对账就都接不上。所以先只认带“合同/协议/订单/项目/单据”的显式标签。
_NO_LABEL_RE = re.compile(
    r"(?:合同编号|合同号|协议编号|框架合同号|订单编号|订单号|项目编号|单据编号|结算单号)"
    # 真实合同号带全角括号：HSYCXS（DD）-2026-0236。字符集不收括号就会被截成 HSYCXS，
    # 同一个系列的所有合同全被塞进一个业务包，归集与对账接不上，用户只能手工重填编号。
    r"[号码:：\s]*([A-Za-z0-9][A-Za-z0-9\-_/（）()．.#·]{3,40})")
_NO_ANY_RE = re.compile(
    r"(?:合同|协议|订单|单据|项目编号|编号|号码)[号码:：\s]*"
    r"([A-Za-z0-9][A-Za-z0-9\-_/（）()．.#·]{3,40})")
# 括号段是真实编号的一部分：HSYCXS（DD）-2026-0236。不认括号就只能拿到 HSYCXS，
# 而扫描件常常只在文件名里带编号，正文一个字都没有。
_NO_SHORT_RE = re.compile(
    r"\b([A-Z]{2,6}(?:[（(][\u4e00-\u9fa5A-Za-z0-9]{1,8}[)）])?[-_]?\d{4,12}(?:[-_]\d{1,4})?)\b")
# 文件名里的“合同/发票”字样太常见，只用显式标签，避免把“专用发票8005”里的 8005 当编号。
_NO_RES_FILE = [(_NO_LABEL_RE, 0.9), (_NO_SHORT_RE, 0.55)]
_NO_RES_BODY = [(_NO_LABEL_RE, 0.92), (_NO_ANY_RE, 0.8), (_NO_SHORT_RE, 0.6)]
# 单价不是金额。合同写「799.05元/湿吨」，总额在两行之后的「合同总金额(人民币) 7990500.00元」；
# 把单价当金额，对账就会把每份合同都报成「金额不符」，业务包上的差额也是假的。
_AMOUNT_LABEL_RE = re.compile(
    # 不含税总额不是结算依据：对账比的是含税总额，取错口径差的就是那笔税额
    r"(?<!不含税)(?:合同总金额|合同总价款|合同总价|合同金额|价税合计|结算总金额|总金额|合计金额|金额合计|总额"
    r"|金额|总价|价款|货款)"
    # 标签与数字之间常隔着「(人民币）」和换行，不接住就等于标签没写
    r"\s*[（(][^（）()]{0,14}[)）]?\s*[为:：]?\s*[¥￥]?\s*([\d,]+(?:\.\d{1,2})?)\s*(?:元|圆|CNY|RMB)?", re.I)
# 第三项是允许回看多少字判断上下文：标签命中只看紧邻的几个字，裸金额看得更宽
_MONEY_RES = [
    (_AMOUNT_LABEL_RE, 0.88, 12),
    (re.compile(r"[¥￥]\s*([\d,]+(?:\.\d{1,2})?)"), 0.9, 24),
    (re.compile(r"([\d,]+(?:\.\d{1,2})?)\s*(?:元|人民币)"), 0.6, 24),
]
# 没有标签的裸金额多看一眼上下文：附近写着税额/保证金/单价，就不是本单据的成交金额
_AMOUNT_AVOID_RE = re.compile(r"不含税|税额|税金|违约金|保证金|押金|费率|折扣|单价|溢价|运费")
# 金额后面紧跟 /元·每 的就是单价，不能当总额用
_UNIT_TAIL_RE = re.compile(r"^(?:元|圆)?\s*[/·每]")
_UNIT_PRICE_RE = re.compile(
    r"([\d,]+(?:\.\d{1,4})?)\s*(?:元|人民币)\s*[/·每]\s*(?:湿)?(?:吨|公斤|千克|立方米|方|箱|件|台|张|度|米)")
# 合同常常只写单价与数量，总额要自己算；数量口径只认这几个单位，且必须唯一，宁缺毋滥
_QTY_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*(?:湿)?(?:吨|公斤|千克|立方米|方|箱|件|台|张)")
_QTY_LABELED_RE = re.compile(r"(?:合同数量|数量|重量|吨数)[为:：\s]*([\d,]+(?:\.\d+)?)")
# 当事方标签：中文标签后的冒号/空格可有可无；To/From/致 必须带显式分隔符且整词匹配，
# 否则「双方一致同意严格遵守本合同」里的「致」会把半句条款当成公司名（真实翻车样本）。
_COMPANY_TAIL = r"[\u4e00-\u9fa5A-Za-z0-9（）()]{4,40}?(?:公司|集团|银行|厂|中心|事务所|研究院|设计院|合作社)"
_COMPANY_LABEL = (
    r"(?:甲方|乙方|买方|卖方|供方|需方|客户|公司名称|销售方|购买方|收款人|付款人|开票单位"
    r"|单位名称|结算对象|收货单位|往来单位)[：:\s]+")
_COMPANY_RE = re.compile(r"(?:" + _COMPANY_LABEL + r"|\b(?:To|From)\b[：:\s]+|致[：:]\s*)"
                           + r"(" + _COMPANY_TAIL + ")", re.I)


# 编号结尾不该悬着半个括号或分隔符：'ABC（' 说明括号里是中文主体，抓到的其实不是完整编号
_NO_TRIM = "（(-—–_/．.·#、,，:：的 \t"


def _clean_no(raw: str) -> str:
    s = (raw or "").strip().rstrip(_NO_TRIM)
    for open_c, close_c in (("（", "）"), ("(", ")")):
        if s.count(open_c) > s.count(close_c):
            s = s[: s.rfind(open_c)].rstrip(_NO_TRIM)
    return s


# 公司名比对忽略空格与全半角括号：「华山云创供应链科技（山西）有限公司」＝「华山云创供应链科技有限公司」
_COMPANY_NOISE_RE = re.compile(r"[\s（）()]")


def _norm_num(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _same_company(a: str, b: str) -> bool:
    return _COMPANY_NOISE_RE.sub("", a or "") == _COMPANY_NOISE_RE.sub("", b or "")


def _parties(body: str) -> list[str]:
    """文中出现过的当事方公司，按出现顺序去重。"""
    out: list[str] = []
    for m in _COMPANY_RE.finditer(body):
        name = m.group(1).strip()
        if name and not any(_same_company(name, x) for x in out):
            out.append(name)
    return out


def _pick_amount(body: str) -> tuple[str, float] | None:
    """带标签的总额优先，紧跟 /元·每 的单价一律跳过。"""
    for rx, conf, window in _MONEY_RES:
        for m in rx.finditer(body):
            if _UNIT_TAIL_RE.match(body[m.end():m.end() + 8]):
                continue
            if _AMOUNT_AVOID_RE.search(body[max(0, m.start() - window):m.start()]):
                continue
            v = _norm_num(m.group(1))
            if v is not None and v > 0:
                return f"{v:.2f}", conf
    return None


def _pick_unit_price(body: str) -> tuple[str, float] | None:
    m = _UNIT_PRICE_RE.search(body)
    if not m:
        return None
    v = _norm_num(m.group(1))
    if v is None or v <= 0:
        return None
    return f"{v:.4f}".rstrip("0").rstrip("."), 0.85


def _derive_total(unit: float, body: str) -> tuple[str, float] | None:
    """没有明示总额时用单价×数量补：合同只写「799.05元/湿吨 + 10000吨」也要有金额可核。

    数量必须唯一（带标签的数量优先），否则宁可空着——凑出来的总额会让对账报出假的「金额不符」。
    """
    if unit <= 0:
        return None
    for rx in (_QTY_LABELED_RE, _QTY_RE):
        vals = {v for v in (_norm_num(x) for x in rx.findall(body)) if v}
        if len(vals) == 1:
            qty = vals.pop()
            if qty > 1:
                return f"{unit * qty:.2f}", 0.7
    return None


def extract(filename: str, text: str) -> Extracted:
    values: dict[str, tuple[str, float]] = {}
    body = text or ""

    m = _DATE_RE.search(filename) or _DATE_RE.search(body[:3000])
    if m:
        y, mo, d = m.groups()
        values[FIELD_DATE] = (f"{y}-{int(mo):02d}-{int(d):02d}", 0.9)

    for src, res, conf in ((filename or "", _NO_RES_FILE, 0.85),
                           (body[:5000], _NO_RES_BODY, 0.75)):
        for rx, c in res:
            m = rx.search(src)
            if not m:
                continue
            no = _clean_no(m.group(1))
            if len(no) < 3:
                continue
            values[FIELD_CONTRACT_NO] = (no, min(conf, c))
            break
        if FIELD_CONTRACT_NO in values:
            break

    unit = _pick_unit_price(body[:6000])
    if unit:
        values[FIELD_UNIT_PRICE] = unit
    amount = _pick_amount(body[:6000])
    if not amount and unit:
        amount = _derive_total(_norm_num(unit[0]) or 0.0, body[:6000])
    if amount:
        values[FIELD_AMOUNT] = amount

    parties = _parties(body[:6000])
    ours = (settings.OUR_COMPANY or "").strip()
    if ours:
        # 本公司名称是全局口径：文档里出现过才算证据，没出现也不硬塞进字段（免得白送字段覆盖率）
        mine = next((x for x in parties if _same_company(x, ours)), "")
        if mine:
            values[FIELD_COMPANY] = (mine, 0.7)
        other = next((x for x in parties if not _same_company(x, ours)), "")
        if other:
            values[FIELD_COUNTERPARTY] = (other, 0.7)
    elif parties:
        # 没填本公司时不再把同一个名字同时写进「公司名称」和「对方单位」：
        # 那样业务包的客户和我方主体同值，合并建议也比不出是不是同一家公司
        values[FIELD_COUNTERPARTY] = (parties[0], 0.7)
        second = next((x for x in parties[1:] if not _same_company(x, parties[0])), "")
        if second:
            values[FIELD_COMPANY] = (second, 0.6)

    return Extracted(values=values)