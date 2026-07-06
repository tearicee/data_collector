#!/usr/bin/env python3
"""基金代號 → 發行商 (經理公司) 對應。

由 MOPS 主檔 fund_master_latest.csv 的「基金中文名稱」前綴推導。發行商短名
即 adapter 註冊表 (base.REGISTRY) 的 key。
"""
from __future__ import annotations
import sys
from pathlib import Path

import pandas as pd

if sys.platform == "win32":
    MASTER_LATEST = Path(r"D:\etf_daily_holdings\fund_master\fund_master_latest.csv")
else:
    MASTER_LATEST = Path("/mnt/d/etf_daily_holdings/fund_master/fund_master_latest.csv")

# 依「基金中文名稱」前綴比對 (較長者需排前面，如『中國信託』先於任何『中』)
# 值為發行商短名 (= adapter key)
ISSUER_PREFIXES: list[tuple[str, str]] = [
    ("中國信託", "中國信託"),
    ("大華銀", "大華"),
    ("元大", "元大"),
    ("國泰", "國泰"),
    ("富邦", "富邦"),
    ("群益", "群益"),
    ("統一", "統一"),
    ("兆豐", "兆豐"),
    ("復華", "復華"),
    ("凱基", "凱基"),
    ("台新", "台新"),
    ("第一金", "第一金"),
    ("永豐", "永豐"),
    ("新光", "新光"),
    ("野村", "野村"),
    ("安聯", "安聯"),
    ("街口", "街口"),
    ("華南永昌", "華南永昌"),
    ("合庫", "合庫"),
    ("景順", "景順"),
    ("富蘭克林", "富蘭克林"),
    ("保德信", "保德信"),
    ("施羅德", "施羅德"),
    ("貝萊德", "貝萊德"),
    ("PGIM", "PGIM"),
    ("摩根大", "摩根"),        # 摩根大美國… → 摩根
    ("摩根", "摩根"),
    ("聯博", "聯博"),
    ("聯邦", "聯邦"),
    ("華頓", "華頓"),
    ("恒生", "恒生"),
    ("標智", "標智"),
    ("玉山", "玉山"),
]


def _issuer_of_name(name: str) -> str | None:
    name = str(name).strip()
    for prefix, issuer in ISSUER_PREFIXES:
        if name.startswith(prefix):
            return issuer
    return None


def all_map(master_csv: Path | None = None) -> dict[str, str]:
    """回傳 {基金代號: 發行商短名}；無法判定者的發行商為 '未知'。"""
    path = master_csv or MASTER_LATEST
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    out: dict[str, str] = {}
    for _, r in df.iterrows():
        code = str(r["基金代號"]).strip()
        issuer = _issuer_of_name(r.get("基金中文名稱", "")) or "未知"
        out[code] = issuer
    return out


def code_to_issuer(code: str, master_csv: Path | None = None) -> str | None:
    return all_map(master_csv).get(str(code).strip())


if __name__ == "__main__":
    m = all_map()
    from collections import Counter
    print(f"總計 {len(m)} 檔")
    for issuer, n in Counter(m.values()).most_common():
        print(f"  {issuer:<10} {n}")
