"""字段提取：公司名称 / 日期 / 编号 / 金额（纯规则、零 token）。"""
from __future__ import annotations

import re
from dataclasses import dataclass

FIELD_COMPANY = "公司名称"
FIELD_CONTRACT_NO = "合同编号"
FIELD_DATE = "日期"
FIELD_AMOUNT = "金额"
FIELD_COUNTERPARTY = "对方单位"


@dataclass
class Extracted:
    values: dict[str, tuple[str, float]]


_DATE_RE = re.compile(r"(20\d{2})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})")
# 显式标签优先：发票上“发票号码/开票日期”很常见，靠位置取胜会把发票号当成合同号，
# 归集与对账就都接不上。所以先只认带“合同/协议/订单/项目/单据”的显式标签。
_NO_LABEL_RE = re.compile(
    r"(?:合同编号|合同号|协议编号|框架合同号|订单编号|订单号|项目编号|单据编号|结算单号)"
    r"[号码:：\s]*([A-Za-z0-9][A-Za-z0-9\-_/]{3,30})")
_NO_ANY_RE = re.compile(
    r"(?:合同|协议|订单|单据|项目编号|编号|号码)[号码:：\s]*"
    r"([A-Za-z0-9][A-Za-z0-9\-_/]{3,30})")
_NO_SHORT_RE = re.compile(r"\b([A-Z]{2,6}[-_]?\d{4,12}(?:[-_]\d{1,4})?)\b")
# 文件名里的“合同/发票”字样太常见，只用显式标签，避免把“专用发票8005”里的 8005 当编号。
_NO_RES_FILE = [(_NO_LABEL_RE, 0.9), (_NO_SHORT_RE, 0.55)]
_NO_RES_BODY = [(_NO_LABEL_RE, 0.92), (_NO_ANY_RE, 0.8), (_NO_SHORT_RE, 0.6)]
_MONEY_RES = [
    (re.compile(r"[¥￥]\s*([\d,]+(?:\.\d{1,2})?)"), 0.9),
    (re.compile(r"(?:金额|总价|合计|价款|货款)[为：:\s]*([\d,]+(?:\.\d{1,2})?)\s*(?:元|圆|CNY|RMB)?", re.I), 0.85),
    (re.compile(r"([\d,]+(?:\.\d{1,2})?)\s*(?:元|人民币)"), 0.6),
]
_COMPANY_RE = re.compile(r"(?:甲方|乙方|买方|卖方|供方|需方|客户|公司名称|销售方|购买方|收款人|付款人|开票单位|单位名称)[：:\s]*([\u4e00-\u9fa5A-Za-z0-9（）()]{4,40}?(?:公司|集团|厂|中心|所|院|行|部))")


def _norm_num(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except ValueError:
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
            if m:
                values[FIELD_CONTRACT_NO] = (m.group(1).strip(), min(conf, c))
                break
        if FIELD_CONTRACT_NO in values:
            break

    for rx, conf in _MONEY_RES:
        m = rx.search(body[:5000])
        if m:
            v = _norm_num(m.group(1))
            if v is not None and v > 0:
                values[FIELD_AMOUNT] = (f"{v:.2f}", conf)
                break

    m = _COMPANY_RE.search(body[:5000])
    if m:
        values[FIELD_COUNTERPARTY] = (m.group(1).strip(), 0.7)
        values.setdefault(FIELD_COMPANY, (m.group(1).strip(), 0.7))

    return Extracted(values=values)