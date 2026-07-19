#!/usr/bin/env python3
"""爬蟲排除清單 (CMoney 主爬蟲 etf_crawler.py + 投信 raw 爬蟲 run_raw.py 共用)。

被排除的商品/發行商：兩支爬蟲在「建立抓取名單」時即濾除，故不抓取、
不列入分類統計、也不會觸發健檢 Discord 告警。

- EXCLUDED_CODES：個別商品 (交易所代號)。
- EXCLUDED_ISSUERS：整家發行商 (issuer_map 短名)，該家全部 ETF 一律排除。

調整時只改本檔即可，兩支爬蟲自動生效。
"""
from __future__ import annotations

import os
import sys

# issuer_map 位於 raw_holdings/ 子目錄，確保可 import
_HERE = os.path.dirname(os.path.abspath(__file__))
_RAW = os.path.join(_HERE, "raw_holdings")
if _RAW not in sys.path:
    sys.path.insert(0, _RAW)

# 個別商品排除 (交易所代號)
EXCLUDED_CODES: set[str] = {
    "00865B",     # 目前不需收集資訊的商品
}

# 整家發行商排除 (issuer_map 短名)：跳警示但暫不需修正的投信
EXCLUDED_ISSUERS: set[str] = {
    "安聯", "富蘭克林", "恒生", "摩根", "標智", "永豐", "玉山",
    "第一金", "聯博", "聯邦", "華南永昌", "華頓", "街口", "貝萊德",
}


def filter_codes(codes, imap: dict | None = None):
    """濾掉排除代號與排除發行商的代號。

    imap: {代號: 發行商} 對照；預設呼叫 issuer_map.all_map() 現算。
          讀不到主檔時退化為僅套用 EXCLUDED_CODES (fail-open，避免因主檔
          暫時讀不到而整批漏抓)。
    回傳 (kept: list[str], dropped: dict[str, str])，dropped 值為原因。
    """
    if imap is None:
        try:
            import issuer_map
            imap = issuer_map.all_map()
        except Exception:                                # noqa: BLE001
            imap = {}
    kept: list[str] = []
    dropped: dict[str, str] = {}
    for c in codes:
        c = str(c).strip()
        if not c:
            continue
        if c in EXCLUDED_CODES:
            dropped[c] = "code"
            continue
        issuer = imap.get(c)
        if issuer in EXCLUDED_ISSUERS:
            dropped[c] = f"issuer:{issuer}"
            continue
        kept.append(c)
    return kept, dropped
