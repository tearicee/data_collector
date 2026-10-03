#!/usr/bin/env python3
"""中國信託投信 (CTBC) 原始持股 adapter — 需 headless browser (playwright)。

中信 www.ctbcinvestments.com.tw 是 Angular SPA + Incapsula WAF，且 API 有嚴格
一次性 token 防爬(主動 requests/page.request/page.evaluate 呼叫一律被拒，只有
頁面自身 render 發出的請求有效)。故用 playwright 模擬使用者操作 + 攔截 response:
  1. 開申購買回清單頁 /Etf/Buyback (過 Incapsula)；select options 給
     交易所代號→FID(如 00891→E0017) 映射
  2. 每檔: select FID → 日期欄位填公告日 → 點「搜尋」→ 頁面用有效 token POST /API/etf/Buyback
     ({token,FID,StartDate}) → 攔其 response (Big5 編碼)
  3. 持股在 Data.Detail[] 各類別(STOCK/FUTURE/BOND...)的 Data[]:
       invtp_/code_/name_/qty_(數量)/weights_(權重%)；MARGIN/CASH 為現金不納入
  4. StartDate 是清單公告日 (= 持股基準日的下一個交易日，回應的 Data[0].公告日)，且只認
     精確日期: 先查下一個交易日，查無資料才查今天。檔名日期用回應的公告日。
  單一 browser 抓完所有中信檔、close() 釋放。
"""
from __future__ import annotations

import json

import pandas as pd

from base import IssuerAdapter, forward_dates, register

BUYBACK_PAGE = "https://www.ctbcinvestments.com.tw/Etf/Buyback"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0"
DATE_INPUT = "input[type=text]"      # 清單頁的日期欄位 (頁面上唯一的文字輸入框)

# invtp_ / 類別 Code → asset_type；未列者(MARGIN/CASH)視為現金不納入
TYPE_MAP = {"STOCK": "股票", "FUTURE": "期貨", "BOND": "債券",
            "ETF": "ETF", "FUND": "基金"}


def _num(v):
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _decode(resp):
    raw = resp.body()
    for enc in ("utf-8", "big5", "cp950"):
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return None


@register("中國信託")
class CTBCAdapter(IssuerAdapter):

    def __init__(self, issuer: str):
        super().__init__(issuer)
        self._pw = None
        self._browser = None
        self._page = None
        self._map: dict[str, str] | None = None      # 交易所代號 → FID

    def _ensure(self):
        if self._page is not None:
            return
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        ctx = self._browser.new_context(user_agent=UA)
        self._page = ctx.new_page()
        self._page.goto(BUYBACK_PAGE, wait_until="networkidle", timeout=60000)
        self._page.wait_for_timeout(2500)
        # select options → {交易所代號: FID}
        opts = self._page.eval_on_selector_all(
            "select.selects option",
            "els => els.map(e => ({v: e.value, t: e.innerText.trim()}))")
        m = {}
        for o in opts:
            v, t = o.get("v"), o.get("t") or ""
            if v and v != "-1" and t:
                code = t.split()[0]                  # "00891 中信關鍵半導體" → 00891
                m[code] = v
        self._map = m

    def fetch(self, fund_code: str, date: str) -> pd.DataFrame:
        self._ensure()
        fid = self._map.get(fund_code)
        if not fid:
            raise ValueError(f"中信清單無此代號 {fund_code}")

        self._page.select_option("select.selects", fid)
        self._page.wait_for_timeout(300)
        # 清單以公告日為準: 先查下一個交易日 (當日收盤後的持股)，沒有才用頁面預設的今天
        detail, announced = [], ""
        for query in forward_dates(date) + [date]:
            data = self._search(query)
            detail = data.get("Detail") or []
            if detail:
                head = (data.get("Data") or [{}])[0]
                announced = str(head.get("公告日") or "").replace("/", "")[:8]
                break

        rows = []
        for sec in detail:
            atype = TYPE_MAP.get(sec.get("Code"))
            if not atype:                            # MARGIN/CASH/其他現金 → 跳過
                continue
            for it in sec.get("Data") or []:
                code = str(it.get("code_") or "").strip()
                if not code:
                    continue
                rows.append({"stock_id": code, "name": it.get("name_"),
                             "asset_type": atype,
                             "shares": _num(it.get("qty_")),
                             "weight": _num(it.get("weights_"))})
        if not rows:
            raise ValueError(f"中信 {fund_code} 無持股明細")

        out = pd.DataFrame(rows)
        out["date"] = announced if len(announced) == 8 else query
        out["fund_code"] = fund_code
        return out

    def _search(self, query: str) -> dict:
        """把頁面日期欄位設成 query (YYYYMMDD) 後按搜尋，回傳 API 回應的 Data。"""
        box = self._page.locator(DATE_INPUT).first
        box.fill(f"{query[:4]}/{query[4:6]}/{query[6:]}")
        box.dispatch_event("change")
        box.dispatch_event("input")
        self._page.wait_for_timeout(300)
        with self._page.expect_response(
                lambda r: "/API/etf/Buyback" in r.url, timeout=20000) as ri:
            self._page.get_by_text("搜尋", exact=True).first.click()
        d = _decode(ri.value) or {}
        data = d.get("Data")
        return data if isinstance(data, dict) else {}

    def close(self):
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()
            self._pw = self._browser = self._page = None
