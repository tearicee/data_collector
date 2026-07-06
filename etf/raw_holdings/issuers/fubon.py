#!/usr/bin/env python3
"""富邦投信 (Fubon) 原始持股 adapter。

資料來源: 富邦 ETF 官網 (ASP.NET) 的資產明細頁 — 純 GET，免 postback:
  GET https://websys.fsit.com.tw/FubonETF/Trade/Assets.aspx
      ?stkId=<交易所代號>&ddate=YYYY/MM/DD&lan=TW

  (註: 同站 Pcf.aspx 需瀏覽器級 postback 無法穩定爬；Assets.aspx 才是可用端點。)

頁面含多個資產類別表格，結構一致的 5 欄:
  [<類型>代碼, <類型>名稱, <數量>, 金額, 權重(%)]
  股票: 股票代碼/股票名稱/股數/金額/權重(%)
  期貨: 期貨代碼/期貨名稱/口數/金額/權重(%)
  債券: 債券代碼/債券名稱/面額/金額/權重(%)
asset_type 由表頭首欄去「代碼/代號」推得；shares=數量欄、weight=權重(%)欄。
指定 ddate 無資料時往前回退數個交易日。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import lxml.html
import pandas as pd

from base import IssuerAdapter, register

ASSETS_URL = "https://websys.fsit.com.tw/FubonETF/Trade/Assets.aspx"
REFERER = "https://websys.fsit.com.tw/FubonETF/"
REQUEST_TIMEOUT = (10, 30)
MAX_LOOKBACK = 6            # 指定日無資料時，往前回退的最多天數


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


@register("富邦")
class FubonAdapter(IssuerAdapter):

    def _get_html(self, fund_code: str, ddate_slash: str) -> str:
        headers = self.headers()
        headers["Referer"] = REFERER
        res = self.session.get(
            ASSETS_URL,
            params={"stkId": fund_code, "ddate": ddate_slash, "lan": "TW"},
            headers=headers, timeout=REQUEST_TIMEOUT)
        res.raise_for_status()
        res.encoding = "utf-8"
        return res.text

    @staticmethod
    def _parse(html: str) -> list[dict]:
        """解析頁面所有持股表格 → 正規化列。"""
        doc = lxml.html.fromstring(html)
        rows: list[dict] = []
        for table in doc.xpath("//table"):
            trs = table.xpath(".//tr")
            if len(trs) < 2:
                continue
            header = [c.text_content().strip() for c in trs[0].xpath("./td|./th")]
            if not header or ("代碼" not in header[0] and "代號" not in header[0]):
                continue
            atype = header[0].replace("代碼", "").replace("代號", "").strip() or "其他"
            # 權重欄 index (含「權重」或「比例」)
            wi = next((i for i, h in enumerate(header) if "權重" in h or "比例" in h),
                      len(header) - 1)
            for tr in trs[1:]:
                tds = [c.text_content().strip() for c in tr.xpath("./td")]
                if len(tds) <= wi or not tds[0]:
                    continue
                # 跳過小計/合計/總計列 (非個別成分)
                if any(k in tds[0] for k in ("合計", "小計", "總計", "合  計")):
                    continue
                rows.append({
                    "stock_id": tds[0],
                    "name": tds[1] if len(tds) > 1 else None,
                    "asset_type": atype,
                    "shares": _num(tds[2]) if len(tds) > 2 else None,  # 股數/口數/面額
                    "weight": _num(tds[wi]),
                })
        return rows

    def fetch(self, fund_code: str, date: str) -> pd.DataFrame:
        base = datetime.strptime(date, "%Y%m%d")
        for back in range(MAX_LOOKBACK + 1):
            d = base - timedelta(days=back)
            ddate = d.strftime("%Y/%m/%d")
            rows = self._parse(self._get_html(fund_code, ddate))
            if rows:
                out = pd.DataFrame(rows)
                out["date"] = d.strftime("%Y%m%d")     # 實際有資料日供命名
                out["fund_code"] = fund_code
                return out
        raise ValueError(f"富邦 {fund_code} 近 {MAX_LOOKBACK} 日查無持股 (自 {date})")
