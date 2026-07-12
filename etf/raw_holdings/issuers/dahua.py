#!/usr/bin/env python3
"""大華銀投信 (UOB Asset Management, uobam) 原始持股 adapter — requests (ServiceStack RPC)。

大華 www.uobam.com.tw/fund/etf/pcf 是 React SPA，後端為 ServiceStack JsonServiceClient
(GET /json/reply/<Request>?args)。兩步:
  ① GET /json/reply/WebSiteStockListRequest → resultP[]，取 ec001==3(ETF) 各項
     result[0].etf002(交易所代號) → result[0].fundID(內部數字碼，如 009815→00648622)。
  ② GET /json/reply/WebSitePcfRequest?fundID=<數字碼>&pcfDate=YYYY/MM/DD
     回 {etf002, datadate:/Date(ms+0800)/, result:[{kind,code,cName,qty,weight,dym,...}]}
     kind: stock(股票)/futures(期貨)/bond(債券)/other(現金保證金→跳過)。
     PCF 回傳「≤ 指定日的最新一筆」(非精確日)，datadate 為實際資料日；故指定今日即得最新。
代號 code 可能帶交易所後綴 (外國股『NVDA US』取第一段；國內『2887』無後綴)；期貨含
契約年月 dym。weight 已是數值。

TLS 註記: 該站伺服器只送葉憑證、漏中間憑證 (中華電信 GCC R46 OV TLS CA)，requests
驗證會 SSLError。因僅抓公開 PCF 資料，故本 adapter 關閉憑證驗證 (verify=False)。
"""
from __future__ import annotations

import re
import urllib3
from datetime import datetime, timedelta, timezone

import pandas as pd

from base import IssuerAdapter, register

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://www.uobam.com.tw/json/reply"
LIST_URL = f"{BASE}/WebSiteStockListRequest"
PCF_URL = f"{BASE}/WebSitePcfRequest"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0",
      "Accept": "application/json", "Accept-Language": "zh-TW"}
MAX_LOOKBACK = 6
KIND_MAP = {"stock": "股票", "futures": "期貨", "future": "期貨",
            "bond": "債券", "etf": "ETF", "fund": "基金"}
_DATE_RE = re.compile(r"/Date\((-?\d+)([+-]\d{4})?\)/")


def _num(v):
    s = str(v).replace(",", "").replace("NT$", "").strip()
    if s in ("", "-", "nan", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_msdate(v) -> str:
    """/Date(1783526400000+0800)/ → YYYYMMDD (台北日曆日)。"""
    m = _DATE_RE.search(str(v))
    if not m:
        return ""
    ms = int(m.group(1))
    if ms < 0:                                   # .NET 最小值 (無資料)
        return ""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc) + timedelta(hours=8)
    return dt.strftime("%Y%m%d")


@register("大華")
class DaHuaAdapter(IssuerAdapter):

    def __init__(self, issuer: str):
        super().__init__(issuer)
        self._map: dict[str, str] | None = None    # 交易所代號 → 內部數字 fundID

    def _ensure_map(self):
        if self._map is not None:
            return
        r = self.session.get(LIST_URL, headers=UA, timeout=(10, 30), verify=False)
        r.raise_for_status()
        m: dict[str, str] = {}
        for item in r.json().get("resultP") or []:
            if item.get("ec001") != 3:             # 3 = ETF
                continue
            res = (item.get("result") or [{}])[0]
            code, fid = res.get("etf002"), res.get("fundID")
            if code and fid:
                m[str(code).strip()] = str(fid).strip()
        self._map = m

    @staticmethod
    def _parse(result: list[dict]) -> list[dict]:
        rows = []
        for x in result:
            kind = str(x.get("kind", "")).strip().lower()
            if kind not in KIND_MAP:               # other = 現金/保證金
                continue
            code = str(x.get("code", "")).strip()
            if not code:
                continue
            rows.append({
                "stock_id": code.split()[0],       # 'NVDA US' → 'NVDA'；'2887' 不變
                "name": str(x.get("cName", "")).strip(),
                "asset_type": KIND_MAP[kind],
                "shares": _num(x.get("qty")),
                "weight": _num(x.get("weight")),
            })
        return rows

    def fetch(self, fund_code: str, date: str) -> pd.DataFrame:
        self._ensure_map()
        fid = self._map.get(fund_code)
        if not fid:
            raise ValueError(f"大華 ETF 清單無此代號 {fund_code}")
        base = datetime.strptime(date, "%Y%m%d")
        for back in range(MAX_LOOKBACK + 1):
            d = base - timedelta(days=back)
            r = self.session.get(PCF_URL, params={"fundID": fid,
                                                  "pcfDate": d.strftime("%Y/%m/%d")},
                                 headers=UA, timeout=(10, 30), verify=False)
            if r.status_code != 200:
                continue
            j = r.json()
            rows = self._parse(j.get("result") or [])
            if rows:
                out = pd.DataFrame(rows)
                out["date"] = _parse_msdate(j.get("datadate")) or d.strftime("%Y%m%d")
                out["fund_code"] = fund_code
                return out
        raise ValueError(f"大華 {fund_code} 近 {MAX_LOOKBACK} 日查無持股 (自 {date})")
