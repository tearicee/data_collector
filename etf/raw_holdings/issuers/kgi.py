#!/usr/bin/env python3
"""凱基投信 (KGI) 原始持股 adapter — requests (免瀏覽器)。

凱基 www.kgifund.com.tw/Fund/RedemptionList (申購買回清單):
  - #AllFundName hidden 存全部基金 [{label, fundID(J017)}]，無交易所代號
  - 持股由 jQuery .load POST /Fund/RedemptionVC {fundID, queryDate} 載入
    (ViewComponent 回 HTML fragment)，回應含交易所代號 + redemption-rest 表格
流程: GET 頁取 fundID 清單 → 對每個 fundID POST RedemptionVC，一次抓完所有凱基
檔並快取 {交易所代號: (rows, date)} (POST 回應才有交易所代號)。
持股在多個 div.redemption-rest，各 h4(股票/期貨/債券) + 表格(代號/名稱/股數/權重%)。
"""
from __future__ import annotations

import html as _html
import json
import re

import lxml.html
import pandas as pd

from base import IssuerAdapter, register

BASE = "https://www.kgifund.com.tw"
PAGE = f"{BASE}/Fund/RedemptionList"
VC = f"{BASE}/Fund/RedemptionVC"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0",
      "Accept-Language": "zh-TW"}
CODE_RE = re.compile(r"\b(00\d{3,4}[A-Z]?)\b")


def _num(v):
    s = str(v).replace(",", "").strip()
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


@register("凱基")
class KGIAdapter(IssuerAdapter):

    def __init__(self, issuer: str):
        super().__init__(issuer)
        self._cache: dict[str, tuple[list, str]] | None = None

    @staticmethod
    def _parse(html: str) -> list[dict]:
        doc = lxml.html.fromstring(html)
        rows = []
        for div in doc.xpath("//div[contains(@class,'redemption-rest')]"):
            h4 = div.xpath(".//*[contains(@class,'redemption-rest__sub-title')]")
            atype = h4[0].text_content().strip() if h4 else "其他"
            for tr in div.xpath(".//table//tr"):
                tds = [c.text_content().strip() for c in tr.xpath("./td")]
                if len(tds) < 4 or not tds[0] or "代號" in tds[0]:
                    continue
                rows.append({"stock_id": tds[0], "name": tds[1],
                             "asset_type": atype,
                             "shares": _num(tds[2]), "weight": _pct(tds[3])})
        return rows

    def _ensure(self):
        if self._cache is not None:
            return
        h = self.session.get(PAGE, headers=UA, timeout=(10, 30)).text
        m = re.search(r'id="AllFundName"[^>]*value="([^"]*)"', h)
        funds = []
        if m:
            try:
                funds = json.loads(_html.unescape(m.group(1)))
            except Exception:                        # noqa: BLE001
                funds = []

        cache = {}
        for f in funds:
            fid = f.get("fundID")
            if not fid:
                continue
            hh = self.session.post(
                VC, data={"fundID": fid, "queryDate": ""},
                headers={**UA, "X-Requested-With": "XMLHttpRequest", "Referer": PAGE},
                timeout=(15, 40)).text
            mcode = CODE_RE.search(hh)
            if not mcode:
                continue
            dm = re.search(r"20\d{2}/\d{2}/\d{2}", hh)
            date = dm.group(0).replace("/", "") if dm else ""
            cache[mcode.group(1)] = (self._parse(hh), date)
        self._cache = cache

    def fetch(self, fund_code: str, date: str) -> pd.DataFrame:
        self._ensure()
        if fund_code not in self._cache:
            raise ValueError(f"凱基清單無此代號 {fund_code}")
        rows, ddate = self._cache[fund_code]
        if not rows:
            raise ValueError(f"凱基 {fund_code} 無持股明細")
        out = pd.DataFrame(rows)
        out["date"] = ddate or date
        out["fund_code"] = fund_code
        return out
