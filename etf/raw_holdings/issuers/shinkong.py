#!/usr/bin/env python3
"""新光投信 (Shin Kong) 原始持股 adapter — 沿用台新端點 (已併入台新投信)。

新光投信已於台新新光金控合併後併入台新投信，原新光官網 etf.skit.com.tw 停用
(權威 DNS 已下線無法連線)。新光系列 ETF (00904 半導體30、009805… 基金中文名仍
以『新光』起始，故 issuer_map 仍歸類新光) 現與台新走同一站同一頁面:
  GET https://www.tsit.com.tw/ETF/Home/Pcf/<交易所代號>?FundType=ALL&DataDate=YYYY-MM-DD
頁面格式與台新完全相同 (div.fund_card + card-header 分類)，故直接繼承 TaishinAdapter
的解析邏輯，僅換發行商標籤 (輸出仍存 新光/ 資料夾，與台新分開)。
已清算/未遷移的舊代號 (如 00742/00925) 端點回 302 → 台新 fetch 回退無資料 → nodata。
"""
from __future__ import annotations

from base import register
from .taishin import TaishinAdapter


@register("新光")
class ShinKongAdapter(TaishinAdapter):
    """新光 ETF 已由台新 tsit.com.tw 站服務，解析邏輯與台新一致。"""
    pass
