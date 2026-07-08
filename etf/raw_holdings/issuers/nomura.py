#!/usr/bin/env python3
"""野村投信 (Nomura) 原始持股 adapter — requests (Angular SPA 後端 JSON API)。

野村 www.nomurafunds.com.tw/ETFWEB/pcf 是 Angular SPA，持股由後端 JSON API 提供:
  POST /API/ETFAPI/api/Fund/GetFundAssets
       body {"FundID": <交易所代號>, "SearchDate": "YYYY-MM-DD"}
FundID 即交易所代號 (00935 等，主動式含 A 尾如 00980A)；SearchDate 僅接受
YYYY-MM-DD (帶斜線/純數字回 400)。回應:
  Entries.Data.FundAsset {Aum, Units, Nav, NavDate}  ← NavDate 為實際資料日
  Entries.Data.Table[]  各表: {TableTitle, Columns[{Name}], Rows[[...]]}
資產表 TableTitle: 股票/期貨/ETF/債券... 欄位隨類別而異，故以 Columns 名稱定位:
  代號=第0欄、名稱=第1欄、數量(股數/口數/面值)、權重(權重%/持債比例)。
非持股表 (TableTitle 空字串=現金摘要、含『特性』=指數比較) 一律跳過。
代號可能帶交易所後綴 (如『1306 JP』) 取第一段；債券代號為 ISIN 無空白。
非交易日 Data 存在但 Table 空 → 往前回退查最近有資料日。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from base import IssuerAdapter, register

URL = "https://www.nomurafunds.com.tw/API/ETFAPI/api/Fund/GetFundAssets"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0",
      "Accept-Language": "zh-TW", "Content-Type": "application/json",
      "Referer": "https://www.nomurafunds.com.tw/ETFWEB/pcf"}
MAX_LOOKBACK = 6
# 持股資產表 (其餘如現金摘要 TableTitle 為空、指數比較含『特性』一律跳過)
KEEP_TITLES = {"股票", "期貨", "債券", "ETF", "基金", "受益證券", "存託憑證"}


def _num(v):
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "nan", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _pct(v):
    s = str(v).replace("%", "").replace(",", "").strip()
    if s in ("", "-", "nan", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


@register("野村")
class NomuraAdapter(IssuerAdapter):

    @staticmethod
    def _col_idx(cols: list[str], keys: tuple[str, ...], default: int | None) -> int | None:
        for j, name in enumerate(cols):
            if any(k in name for k in keys):
                return j
        return default

    @classmethod
    def _parse(cls, tables: list[dict]) -> list[dict]:
        rows = []
        for tb in tables:
            title = str(tb.get("TableTitle", "")).strip()
            if title not in KEEP_TITLES:
                continue
            cols = [str(c.get("Name", "")).strip() for c in (tb.get("Columns") or [])]
            wi = cls._col_idx(cols, ("權重", "比例", "比重"), len(cols) - 1)
            qi = cls._col_idx(cols, ("股數", "口數", "面值", "面額", "數量"), 2)
            for r in tb.get("Rows") or []:
                if not r or wi is None or wi >= len(r) or not str(r[0]).strip():
                    continue
                sid = str(r[0]).split()[0]           # '1306 JP' → '1306'
                if any(k in str(r[0]) for k in ("合計", "小計", "總計")):
                    continue
                rows.append({
                    "stock_id": sid,
                    "name": str(r[1]).strip() if len(r) > 1 else "",
                    "asset_type": title,
                    "shares": _num(r[qi]) if qi is not None and qi < len(r) else None,
                    "weight": _pct(r[wi]),
                })
        return rows

    def fetch(self, fund_code: str, date: str) -> pd.DataFrame:
        base = datetime.strptime(date, "%Y%m%d")
        for back in range(MAX_LOOKBACK + 1):
            d = base - timedelta(days=back)
            r = self.session.post(URL, json={"FundID": fund_code,
                                             "SearchDate": d.strftime("%Y-%m-%d")},
                                  headers=self.headers() | UA, timeout=(10, 30))
            if r.status_code != 200:
                continue
            data = ((r.json().get("Entries") or {}).get("Data")) or {}
            tables = data.get("Table") or []
            rows = self._parse(tables)
            if rows:
                navdate = str((data.get("FundAsset") or {}).get("NavDate") or "").strip()
                out = pd.DataFrame(rows)
                out["date"] = navdate.replace("/", "") or d.strftime("%Y%m%d")
                out["fund_code"] = fund_code
                return out
        raise ValueError(f"野村 {fund_code} 近 {MAX_LOOKBACK} 日查無持股 (自 {date})")
