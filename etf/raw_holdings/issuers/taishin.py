#!/usr/bin/env python3
"""台新投信 (Taishin) 原始持股 adapter — requests (最簡單，URL 自帶代號+日期)。

台新 www.tsit.com.tw ETF 申購買回清單，URL 直接帶交易所代號與日期:
  GET /ETF/Home/Pcf/<交易所代號>?FundType=ALL&DataDate=YYYY-MM-DD
持股 server render 於多個 div.fund_card，各有 card-header(股票/期貨/債券) + 表格:
  股票: 代號/名稱/股數/持股權重   期貨: 期貨代號/名稱/契約年月/口數/持股權重
代號帶交易所後綴(如『3017 TT』)取第一段；跳過摘要卡與『X合計』列。
免 ETF 清單映射 (URL 帶代號)。指定日無資料往前回退。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import lxml.html
import pandas as pd

from base import IssuerAdapter, register

BASE = "https://www.tsit.com.tw/ETF/Home/Pcf"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0",
      "Accept-Language": "zh-TW"}
MAX_LOOKBACK = 6
KEEP_TYPES = {"股票", "期貨", "債券", "ETF", "基金"}


def _num(v):
    s = str(v).replace(",", "").replace("TWD", "").strip()
    if s in ("", "-", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _pct(v):
    s = str(v).replace("%", "").replace(",", "").strip()
    if s in ("", "-", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


@register("台新")
class TaishinAdapter(IssuerAdapter):

    @staticmethod
    def _parse(html: str) -> list[dict]:
        doc = lxml.html.fromstring(html)
        rows = []
        for card in doc.xpath("//div[contains(@class,'fund_card')]"):
            hdr = card.xpath(".//div[contains(@class,'card-header')]")
            atype = (hdr[0].text_content().strip() if hdr else "").split()[0] if hdr else ""
            if atype not in KEEP_TYPES:
                continue
            for tb in card.xpath(".//table"):
                trs = tb.xpath(".//tr")
                if not trs:
                    continue
                header = [c.text_content().strip() for c in trs[0].xpath("./th|./td")]
                wi = next((j for j, x in enumerate(header) if "權重" in x or "比例" in x),
                          len(header) - 1)
                qi = next((j for j, x in enumerate(header)
                           if any(k in x for k in ["股數", "口數", "面額", "數量"])), 2)
                for tr in trs[1:]:
                    tds = [c.text_content().strip() for c in tr.xpath("./td")]
                    if len(tds) <= wi or not tds[0]:
                        continue
                    sid = tds[0].split()[0]          # '3017 TT' → '3017'
                    if any(k in tds[0] for k in ("合計", "小計", "總計")):
                        continue
                    rows.append({
                        "stock_id": sid,
                        "name": tds[1] if len(tds) > 1 else "",
                        "asset_type": atype,
                        "shares": _num(tds[qi]) if qi < len(tds) else None,
                        "weight": _pct(tds[wi]),
                    })
        return rows

    def fetch(self, fund_code: str, date: str) -> pd.DataFrame:
        base = datetime.strptime(date, "%Y%m%d")
        for back in range(MAX_LOOKBACK + 1):
            d = base - timedelta(days=back)
            r = self.session.get(f"{BASE}/{fund_code}",
                                 params={"FundType": "ALL",
                                         "DataDate": d.strftime("%Y-%m-%d")},
                                 headers=UA, timeout=(10, 30))
            if r.status_code != 200:
                continue
            rows = self._parse(r.text)
            if rows:
                out = pd.DataFrame(rows)
                out["date"] = d.strftime("%Y%m%d")
                out["fund_code"] = fund_code
                return out
        raise ValueError(f"{self.issuer} {fund_code} 近 {MAX_LOOKBACK} 日查無持股 (自 {date})")
