#!/usr/bin/env python3
"""群益投信 (Capital) 原始持股 adapter — 需 headless browser (playwright)。

群益 www.capitalfund.com.tw 是 Angular Universal(SSR) + Incapsula WAF，持股 API
需在瀏覽器環境(過 Incapsula JS challenge)才能存取。故用 playwright headless:
  1. 開 buyback 頁一次 → 過 Incapsula、建立 cookie
  2. 同 context 用 page.request 打真實後端 (前綴 /CFWeb/api，非 /api):
       POST /CFWeb/api/etf/items          → [{fundNo, stockNo, shortName}] 映射
       POST /CFWeb/api/etf/buyback  body={"fundId":<fundNo>,"date":null}
         → data.stocks / data.futures / data.bonds (持股)；回應為 Big5 編碼
  單一 browser 抓完所有群益檔，close() 時釋放。

正規化: stocks(股票 stocNo/stocName/share/weight)、futures(期貨 txEname/txDesc/lot/weight)、
bonds(債券)；assets(保證金現金)/rps(附買回) 屬現金管理，不納入成分。
"""
from __future__ import annotations

import json
import sys
import time

import pandas as pd

from base import IssuerAdapter, register

BASE = "https://www.capitalfund.com.tw/CFWeb/api"
BUYBACK_PAGE = "https://www.capitalfund.com.tw/etf/transaction/buyback"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0"
POST_TIMEOUT_MS = 30000     # 群益 API 單次請求逾時
POST_RETRIES = 2            # 逾時重試次數 (2026-10 一週內 3 次單檔逾時都是偶發，重試就好)
RETRY_WAIT_S = 5


def _decode_json(resp):
    """群益 API 回應為 Big5 編碼，playwright .json() 會失敗，故手動解碼。"""
    raw = resp.body()
    for enc in ("utf-8", "big5", "cp950"):
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return None


@register("群益")
class CapitalAdapter(IssuerAdapter):

    def __init__(self, issuer: str):
        super().__init__(issuer)
        self._pw = None
        self._browser = None
        self._page = None
        self._map: dict[str, str] | None = None   # stockNo → fundId (=fundNo)

    def _ensure(self):
        if self._page is not None:
            return
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        ctx = self._browser.new_context(user_agent=UA)
        self._page = ctx.new_page()
        # 開一次 buyback 頁過 Incapsula (建立 cookie)
        self._page.goto(BUYBACK_PAGE, wait_until="networkidle", timeout=60000)
        self._page.wait_for_timeout(1500)
        # 建 stockNo → fundId 映射
        items = (self.post(f"{BASE}/etf/items", {}) or {}).get("data") or []
        self._map = {x["stockNo"]: x["fundNo"] for x in items
                     if x.get("stockNo") and x.get("fundNo")}

    def post(self, url: str, body: dict, retries: int = POST_RETRIES) -> dict | None:
        """群益 API 偶發 30 秒逾時，逾時就等一下再試；其餘錯誤照丟。pcf/sources.py 也走這裡。"""
        for attempt in range(retries + 1):
            try:
                r = self._page.request.post(url, data=json.dumps(body), timeout=POST_TIMEOUT_MS,
                                            headers={"Content-Type": "application/json",
                                                     "Referer": BUYBACK_PAGE})
                return _decode_json(r)
            except Exception as e:                       # noqa: BLE001  playwright TimeoutError 無共同基底可 catch
                if "Timeout" not in type(e).__name__ or attempt >= retries:
                    raise
                print(f"[raw] 群益 {url.rsplit('/', 1)[-1]} 逾時，{RETRY_WAIT_S}s 後重試 "
                      f"({attempt + 1}/{retries})", file=sys.stderr)
                time.sleep(RETRY_WAIT_S)
        return None

    def fetch(self, fund_code: str, date: str) -> pd.DataFrame:
        self._ensure()
        fund_id = self._map.get(fund_code)
        if not fund_id:
            raise ValueError(f"群益清單無此代號 {fund_code}")

        d = self.post(f"{BASE}/etf/buyback", {"fundId": fund_id, "date": None}) or {}
        data = d.get("data") or {}
        pcf = data.get("pcf") or {}

        rows = []
        # 股票
        for s in data.get("stocks") or []:
            rows.append({"stock_id": str(s.get("stocNo") or "").strip(),
                         "name": s.get("stocName"), "asset_type": "股票",
                         "shares": s.get("share"), "weight": s.get("weight")})
        # 期貨
        for f in data.get("futures") or []:
            rows.append({"stock_id": str(f.get("txEname") or f.get("txDesc") or "").strip(),
                         "name": f.get("txDesc"), "asset_type": "期貨",
                         "shares": f.get("lot"), "weight": f.get("weight")})
        # 債券 (bondNo/bondName/weight/faceValue)
        for b in data.get("bonds") or []:
            rows.append({"stock_id": str(b.get("bondNo") or "").strip(),
                         "name": b.get("bondName"), "asset_type": "債券",
                         "shares": b.get("faceValue"), "weight": b.get("weight")})
        rows = [r for r in rows if r["stock_id"]]
        if not rows:
            raise ValueError(f"群益 {fund_code} 無持股明細")

        out = pd.DataFrame(rows)
        d1 = str(pcf.get("date1") or "")            # "2026-07-07"
        out["date"] = d1.replace("-", "")[:8] or date
        out["fund_code"] = fund_code
        return out

    def close(self):
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()
            self._pw = self._browser = self._page = None
