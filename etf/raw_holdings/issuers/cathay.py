#!/usr/bin/env python3
"""國泰投信 (Cathay) 原始持股 adapter。

資料來源: 國泰投信 ETF 官網 (Angular SPA) 後端 API
  base: https://cwapi.cathaysite.com.tw/
  1. GET api/ETF/GetETFList?CurrentPage=N&PerPage=10  (翻頁) → 建立
     交易所代號(stockCode) → 國泰內部代號(fundCode) 映射；國泰約 41 檔。
  2. GET api/ETF/GetETFInfoMain?fundCode=<內部代號> → navDate (持股基準日)
  3. GET api/ETF/GetETFDetail{Stock|Bond|ETF|Future|Fund|Bal}List
        ?FundCode=<內部代號>&SearchDate=YYYY/MM/DD → 各資產類別持股

各類別欄位不同 (逆向自 detail chunk 88/107 的 GetETFDetailStockList({FundCode,SearchDate})):
  Stock : stockCode/stockName/volumn(股數)/weights(權重%)
  Bond  : bondNo/bondName/parValue(面額)/ntMkval(淨值比%)
  Future: ftNo/ftName/volumn(口數)/ntMkval(淨值比%)
  ETF/Fund/Bal: 欄位不定 → 通用啟發式解析
注意參數名為大寫 FundCode/SearchDate，SearchDate 須 YYYY/MM/DD 格式。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from base import IssuerAdapter, register

API = "https://cwapi.cathaysite.com.tw/api/ETF"
REFERER = "https://www.cathaysite.com.tw/ETF"
REQUEST_TIMEOUT = (10, 30)

# 各類別欄位映射 (端點, asset_type, {id,name,shares,weight})；shares/weight 對應
# 欄名不存在時該值留空 (None)。
#   Stock/Bond/Future 的 weight 欄 (weights/ntMkval) 是權重%；
#   Fund 的 ntMkval 實為「持有金額」語意不一致，故不當 weight，僅記代號/名稱。
#   BalList 為現金/資產配置摘要 (item=現金…，非成分股)，故不納入。
#   ETFList 罕見且無穩定樣本欄位，暫不納入 (日後有樣本再補)。
KNOWN_ENDPOINTS = [
    ("GetETFDetailStockList", "股票",
     {"id": "stockCode", "name": "stockName", "shares": "volumn", "weight": "weights"}),
    ("GetETFDetailBondList", "債券",
     {"id": "bondNo", "name": "bondName", "shares": "parValue", "weight": "ntMkval"}),
    ("GetETFDetailFutureList", "期貨",
     {"id": "ftNo", "name": "ftName", "shares": "volumn", "weight": "ntMkval"}),
    ("GetETFDetailFundList", "基金",
     {"id": "fnNo", "name": "fnName", "shares": "", "weight": ""}),
]


def _num(v):
    """去逗號的數字字串轉 float；空/無效回 None。"""
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


@register("國泰")
class CathayAdapter(IssuerAdapter):

    def __init__(self, issuer: str):
        super().__init__(issuer)
        self._map: dict[str, str] | None = None      # stockCode → 內部 fundCode

    def _get(self, endpoint: str, params: dict) -> dict:
        headers = self.headers()
        headers["Referer"] = REFERER
        res = self.session.get(f"{API}/{endpoint}", params=params,
                               headers=headers, timeout=REQUEST_TIMEOUT)
        res.raise_for_status()
        return res.json()

    def _load_map(self) -> dict[str, str]:
        if self._map is not None:
            return self._map
        m: dict[str, str] = {}
        page = 1
        while page <= 12:                            # 上限保護 (約 5 頁)
            d = self._get("GetETFList", {"CurrentPage": page, "PerPage": 10})
            rows = d.get("result") or []
            if not rows:
                break
            for x in rows:
                sc = str(x.get("stockCode") or "").strip()
                fc = str(x.get("fundCode") or "").strip()
                if sc and fc:
                    m[sc] = fc
            total = d.get("totalCount") or 0
            if len(m) >= total or len(rows) < 10:
                break
            page += 1
        self._map = m
        return m

    def _resolve_date(self, internal_code: str, date: str) -> str:
        """回傳持股基準日 YYYY/MM/DD。優先用 InfoMain.navDate，否則用傳入 date。"""
        try:
            d = self._get("GetETFInfoMain", {"fundCode": internal_code})
            nav = (d.get("result") or {}).get("navDate")
            if nav:
                return str(nav).strip()               # 已是 YYYY/MM/DD
        except Exception:                             # noqa: BLE001
            pass
        return f"{date[:4]}/{date[4:6]}/{date[6:8]}"

    def fetch(self, fund_code: str, date: str) -> pd.DataFrame:
        m = self._load_map()
        internal = m.get(fund_code)
        if not internal:
            raise ValueError(f"國泰清單無此代號 {fund_code} (可能不在官網揭露)")

        search_date = self._resolve_date(internal, date)
        params = {"FundCode": internal, "SearchDate": search_date}

        rows = []
        for endpoint, atype, cols in KNOWN_ENDPOINTS:
            for r in self._fetch_list(endpoint, params):
                sid = str(r.get(cols["id"]) or "").strip()
                if not sid:
                    continue
                rows.append({
                    "stock_id": sid,
                    "name": r.get(cols["name"]),
                    "asset_type": atype,
                    "shares": _num(r.get(cols["shares"])),
                    "weight": _num(r.get(cols["weight"])),
                })

        if not rows:
            raise ValueError(f"國泰 {fund_code} 無持股明細 (SearchDate={search_date})")

        out = pd.DataFrame(rows)
        out["date"] = search_date.replace("/", "")    # YYYYMMDD 供命名
        out["fund_code"] = fund_code
        return out

    def _fetch_list(self, endpoint: str, params: dict) -> list:
        try:
            d = self._get(endpoint, params)
        except Exception:                             # noqa: BLE001
            return []
        if str(d.get("returnCode")) != "2000":
            return []
        r = d.get("result")
        return r if isinstance(r, list) else []
