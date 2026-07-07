#!/usr/bin/env python3
"""復華投信 (Fuh Hwa) 原始持股 adapter — requests (免瀏覽器)。

復華 www.fhtrust.com.tw 是 petite-vue，無 WAF，requests 即可:
  1. GET / (cookie)
  2. GET /api/fundList → 每筆 etf002(交易所代號) + fundID(內部碼 ETF21) 建映射
  3. GET /api/assets?fundID=<內部碼>&qDate=YYYY/MM/DD → 每日完整持股
     result[0].detail[]: ftype(股票/期貨/債券) / stockid / stockname /
     qshare(股數) / mvalue(市值) / prate_addaccint(權重%)；dDate=實際資料日
     『其他資產/現金/保證金』屬現金不納入。

注意: assets 參數是大寫 fundID + qDate (小寫 fundId 會回首頁 HTML)。
/api/stockhold 是月報前十大且無代號，不用；assets 才是每日完整。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from base import IssuerAdapter, register

BASE = "https://www.fhtrust.com.tw"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0"}
MAX_LOOKBACK = 6
SKIP_TYPES = {"其他資產", "現金", "保證金", "應收付證券款", "應收付款"}
import re  # noqa: E402
CODE_RE = re.compile(r"^\d{4,6}[A-Z]?$")


def _num(v):
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _pct(v):
    if v is None:
        return None
    s = str(v).replace("%", "").replace(",", "").strip()
    if s in ("", "-", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


@register("復華")
class FuhHwaAdapter(IssuerAdapter):

    def __init__(self, issuer: str):
        super().__init__(issuer)
        self._map: dict[str, str] | None = None      # 交易所代號 → fundID

    def _ensure(self):
        if self._map is not None:
            return
        self.session.get(BASE, headers=UA, timeout=(10, 30))          # cookie
        fl = self.session.get(f"{BASE}/api/fundList", headers=UA,
                              timeout=(10, 30)).json().get("result") or []
        m = {}
        for rec in fl:
            code = str(rec.get("etf002") or "").strip()
            fid = str(rec.get("fundID") or "").strip()
            if CODE_RE.match(code) and fid:
                m.setdefault(code, fid)
        self._map = m

    def fetch(self, fund_code: str, date: str) -> pd.DataFrame:
        self._ensure()
        fid = self._map.get(fund_code)
        if not fid:
            raise ValueError(f"復華清單無此代號 {fund_code}")

        base = datetime.strptime(date, "%Y%m%d")
        for back in range(MAX_LOOKBACK + 1):
            q = (base - timedelta(days=back)).strftime("%Y/%m/%d")
            r = self.session.get(f"{BASE}/api/assets",
                                 params={"fundID": fid, "qDate": q},
                                 headers=UA, timeout=(10, 30))
            try:
                res = (r.json().get("result") or [{}])[0]
            except Exception:                        # noqa: BLE001
                res = {}
            detail = res.get("detail") or []
            rows = []
            for it in detail:
                ftype = str(it.get("ftype") or "").strip()
                if ftype in SKIP_TYPES:
                    continue
                sid = str(it.get("stockid") or "").strip()
                if not sid:
                    continue
                rows.append({"stock_id": sid, "name": it.get("stockname"),
                             "asset_type": ftype,
                             "shares": _num(it.get("qshare")),
                             "weight": _pct(it.get("prate_addaccint"))})
            if rows:
                out = pd.DataFrame(rows)
                ddate = str(res.get("dDate") or "").replace("/", "")[:8]
                out["date"] = ddate or (base - timedelta(days=back)).strftime("%Y%m%d")
                out["fund_code"] = fund_code
                return out
        raise ValueError(f"復華 {fund_code} 近 {MAX_LOOKBACK} 日查無持股 (自 {date})")
