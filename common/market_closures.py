#!/usr/bin/env python3
"""台股臨時休市清單 (健檢 / 資料完整性稽核共用)。

TWSE 年度行事曆 (含 FinMind 的 TaiwanStockTradingDate) 只反映「事先公布」的
假期，不會回填颱風假等臨時休市日。這類日子當天全市場無成交，各資料集本來就
不會有檔案，若照行事曆比對就會被誤判為「資料缺漏」。

本檔列出這些臨時休市日，讓健檢與稽核把該日視為非交易日、不計入缺漏、
不發告警。新增臨時休市日時只改本檔即可。

判定依據 (2026-07-10 為例)：
  - 逐股票下載日誌顯示 3115/3115 股皆無資料
  - 期貨 tick 僅 00:54~04:58 夜盤 58,309 筆，日盤完全未開
    (對照 07-09 為 00:09~13:44 / 873,307 筆)
"""
from __future__ import annotations

from datetime import date, timedelta

# 臨時休市日 (非事先公布之假期，如颱風假)。值為當日休市原因，僅供人閱讀。
UNSCHEDULED_CLOSURES: dict[str, str] = {
    "2026-07-10": "臨時休市 (全市場無成交；期貨僅夜盤，日盤未開)",
}


def is_closure(d) -> bool:
    """該日是否為臨時休市日。d 可為 date 或 'YYYY-MM-DD' 字串。"""
    key = d.isoformat() if isinstance(d, date) else str(d)
    return key in UNSCHEDULED_CLOSURES


def is_trading_day(d) -> bool:
    """該日是否為可預期有資料的交易日 (排除週末與臨時休市)。

    不含年度國定假期 — 那些 TWSE 行事曆已涵蓋，呼叫端若已比對行事曆，
    再疊加本函式即可補上臨時休市這一層。
    """
    dt = date.fromisoformat(str(d)) if not isinstance(d, date) else d
    return dt.weekday() < 5 and not is_closure(dt)


def filter_trading_days(days):
    """濾掉週末與臨時休市日，回傳 (kept, dropped)。

    dropped 為 {日期字串: 原因}，供稽核報表說明「為何不算缺漏」。
    """
    kept: list = []
    dropped: dict[str, str] = {}
    for d in days:
        dt = date.fromisoformat(str(d)) if not isinstance(d, date) else d
        key = dt.isoformat()
        if dt.weekday() >= 5:
            dropped[key] = "週末"
        elif is_closure(dt):
            dropped[key] = UNSCHEDULED_CLOSURES[key]
        else:
            kept.append(d)
    return kept, dropped


def last_business_day(today):
    """今天之前最近一個「應有資料」的營業日 (跳過週末與臨時休市)。"""
    d = today - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d
