#!/usr/bin/env python3
"""元大投信 (YuantaETFs) 原始持股 adapter。

資料來源: 元大 ETF 官網 (Nuxt SPA) 的後端 API
  GET https://etfapi.yuantaetfs.com/ectranslation/api/bridge
  query: APIType=ETFAPI & FuncId=PCF/Daily & ticker=<代號>
         + 前端固定共用參數 (CompanyName/AppName/Device/Platform/DeviceId/PageName)
  回傳 JSON: {PCF{trandate,fundname,...}, InKind{FundComposition}, FundWeights{StockWeights,...}, Memo}

持股取 FundWeights 之四類並合併 (以 asset_type 標記):
  StockWeights(股票) / BondWeights(債券) / ETFWeights(ETF) / FutureWeights(期貨)
  共同欄位 code(成分代號) / name / weights(權重%) / qty(持有數量)
資料日期取 PCF.trandate。股票型 ETF 用 StockWeights；債券/連結/期貨型用對應類別。

逆向過程: 官網 $getAPI plugin 以 GET 打 getBaseUrl(=etfapi.../ectranslation)+"/api/bridge"，
帶 APIType/FuncId 等 query，回 {ResultCode,Data} 或直接資料物件。
"""
from __future__ import annotations

import pandas as pd

from base import IssuerAdapter, register

BRIDGE_URL = "https://etfapi.yuantaetfs.com/ectranslation/api/bridge"
REFERER = "https://www.yuantaetfs.com/"
REQUEST_TIMEOUT = (10, 30)

# 前端 $getAPI 對 APIType=ETFAPI 帶的固定共用參數
COMMON_PARAMS = {
    "APIType": "ETFAPI",
    "CompanyName": "YUANTAFUNDS",
    "AppName": "ETF",
    "Device": "3",
    "Platform": "ETF",
    "DeviceId": "null",
}


@register("元大")
class YuantaAdapter(IssuerAdapter):

    def _get_pcf(self, fund_code: str) -> dict:
        params = dict(COMMON_PARAMS)
        params["FuncId"] = "PCF/Daily"
        params["PageName"] = f"/product/detail/{fund_code}/ratio"
        params["ticker"] = fund_code
        headers = self.headers()
        headers["Referer"] = REFERER
        res = self.session.get(BRIDGE_URL, params=params, headers=headers,
                               timeout=REQUEST_TIMEOUT)
        res.raise_for_status()
        data = res.json()
        # 相容兩種回傳: 直接資料物件 或 {ResultCode,Data} 包裝
        if isinstance(data, dict) and "Data" in data and "PCF" not in data:
            if data.get("ResultCode") not in (0, None):
                raise ValueError(f"元大 API ResultCode={data.get('ResultCode')} "
                                 f"{data.get('ResultMsg')}")
            data = data["Data"]
        return data

    def fetch(self, fund_code: str, date: str) -> pd.DataFrame:
        data = self._get_pcf(fund_code)
        if not isinstance(data, dict):
            raise ValueError("元大 API 回傳格式非預期")

        pcf = data.get("PCF") or {}
        trandate = str(pcf.get("trandate") or "").strip()

        fw = data.get("FundWeights") or {}
        rows = []
        for key, atype in (("StockWeights", "股票"), ("BondWeights", "債券"),
                           ("ETFWeights", "ETF"), ("FutureWeights", "期貨")):
            for r in (fw.get(key) or []):
                rows.append({
                    "stock_id": str(r.get("code") or "").strip(),
                    "name": r.get("name"),
                    "asset_type": atype,
                    "shares": r.get("qty"),       # 持有數量 (股票=股數/債券=面額/期貨=口數)
                    "weight": r.get("weights"),   # 權重(%)
                })
        if not rows:
            # 完全無持股明細 (當日尚未公告或非可揭露型)
            raise ValueError(f"元大 {fund_code} 無持股明細 (trandate={trandate})")

        out = pd.DataFrame(rows)
        # 以來源實際交易日為準 (可能與傳入 date 不同 → 由 run_raw 依此命名)
        out["date"] = trandate or date
        out["fund_code"] = fund_code
        return out
