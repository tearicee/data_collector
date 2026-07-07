#!/usr/bin/env python3
"""兆豐投信 (Mega) 原始持股 adapter — requests (ASP.NET postback，免瀏覽器)。

兆豐 www.megafunds.com.tw/MEGA/etf/trade_pcf.aspx 是 ASP.NET WebForms:
  - select#fund_id 用內部 ID(5,16,17…) + 基金中文名(無交易所代號)，查詢按鈕
    postback；頁面 divStockCash(股票)/divFuture(期貨) 為 server render 表格。
  - 切換檔案需 POST(__VIEWSTATE + fund_id + button1)；一個 GET 的 __VIEWSTATE
    可重複 POST 不同 fund_id。POST 後頁面顯示該檔交易所代號。
流程: GET 取 hidden + fund_id 清單 → 對每個 fund_id POST，一次抓完所有兆豐檔
並快取 {交易所代號: (rows, date)}；fetch 從快取取。
divStockCash: 股票代號/股票名稱/股數/持股權重；divFuture: 期貨。
債券/平衡型(如 00957B)不揭露個別持股(divStockCash 空) → nodata。
"""
from __future__ import annotations

import re
from datetime import datetime

import lxml.html
import pandas as pd

from base import IssuerAdapter, register

URL = "https://www.megafunds.com.tw/MEGA/etf/trade_pcf.aspx"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0",
      "Accept-Language": "zh-TW"}
PX = "ctl00$ContentPlaceHolder1$"
CODE_RE = re.compile(r"\b(00\d{3,4}[A-Z]?)\b")
# (div id, asset_type)
HOLD_DIVS = [("divStockCash", "股票"), ("divFuture", "期貨"), ("divBondCash", "債券")]


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


@register("兆豐")
class MegaAdapter(IssuerAdapter):

    def __init__(self, issuer: str):
        super().__init__(issuer)
        self._cache: dict[str, tuple[list, str]] | None = None

    @staticmethod
    def _hidden(html: str, name: str) -> str:
        m = re.search(r'name="' + re.escape(name) + r'"[^>]*value="([^"]*)"', html)
        return m.group(1) if m else ""

    @staticmethod
    def _page_date(html: str) -> str:
        """取頁面 PCF 基準日 (排除今天，取最新)。"""
        today = datetime.now().strftime("%Y/%m/%d")
        ds = sorted(set(re.findall(r"20\d{2}/\d{2}/\d{2}", html)))
        ds = [d for d in ds if d != today]
        return ds[-1].replace("/", "") if ds else ""

    @staticmethod
    def _parse_holdings(html: str) -> list[dict]:
        doc = lxml.html.fromstring(html)
        rows = []
        for div_id, atype in HOLD_DIVS:
            for div in doc.xpath(f"//*[@id='{div_id}']"):
                for tr in div.xpath(".//tr"):
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
        h = self.session.get(URL, headers=UA, timeout=(10, 30)).text
        vs = self._hidden(h, "__VIEWSTATE")
        vg = self._hidden(h, "__VIEWSTATEGENERATOR")
        ve = self._hidden(h, "__VIEWSTATEENCRYPTED")
        sel = re.findall(r'<select[^>]*id="fund_id".*?</select>', h, re.S)
        fund_opts = re.findall(r'value="(\d+)"[^>]*>([^<]*)', sel[0]) if sel else []

        cache = {}
        for fid, _name in fund_opts:
            data = {"__VIEWSTATE": vs, "__VIEWSTATEGENERATOR": vg,
                    "__VIEWSTATEENCRYPTED": ve,
                    PX + "category_id": "", PX + "fund_id": fid,
                    PX + "button1": "查 詢"}
            hh = self.session.post(URL, data=data,
                                   headers={**UA, "Referer": URL}, timeout=(15, 40)).text
            mcode = CODE_RE.search(hh)
            if not mcode:
                continue
            cache[mcode.group(1)] = (self._parse_holdings(hh), self._page_date(hh))
        self._cache = cache

    def fetch(self, fund_code: str, date: str) -> pd.DataFrame:
        self._ensure()
        if fund_code not in self._cache:
            raise ValueError(f"兆豐清單無此代號 {fund_code}")
        rows, ddate = self._cache[fund_code]
        if not rows:
            raise ValueError(f"兆豐 {fund_code} 無個別持股明細 (債券/平衡型)")
        out = pd.DataFrame(rows)
        out["date"] = ddate or date
        out["fund_code"] = fund_code
        return out
