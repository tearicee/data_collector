#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
櫃買中心「產業價值鏈資訊平台」(ic.tpex.org.tw) 產業鏈成員下載 (上市/上櫃/興櫃共用的官方分類)
================================================================
  用途：題材概念股對照表的官方依據 (例：資通訊安全產業鏈 5500)。公司分類不從新聞猜，從官方鏈 + MOPS 主要經營業務。
  用法：python news/download_ic_chain.py 5500            # 單一產業鏈
        python news/download_ic_chain.py --list         # 列出所有產業鏈代碼與名稱
  輸出：D:/mops/news/mapping/官方產業鏈/產業鏈_{代碼}_{名稱}_{YYYYMMDD}.csv  (細項, 代號, 名稱)
"""
import csv
import html
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests

BASE = "https://ic.tpex.org.tw"
OUT = Path("/mnt/d/mops/news/mapping/官方產業鏈")
UA = {"User-Agent": "Mozilla/5.0"}


def get(url: str) -> str:
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"            # 伺服器沒宣告 charset，requests 會猜成 latin-1 變亂碼
    return r.text


def list_chains() -> list:
    idx = get(f"{BASE}/")
    out = []
    for ic in sorted(set(re.findall(r"introduce\.php\?ic=([A-Za-z0-9]+)", idx))):
        t = re.search(r"<title>[^<]*>\s*([^<]+?)產業鏈簡介", get(f"{BASE}/introduce.php?ic={ic}"))
        out.append((ic, t.group(1).strip() if t else ""))
        time.sleep(0.3)
    return out


def chain_members(ic: str) -> tuple:
    page = get(f"{BASE}/introduce.php?ic={ic}")
    name = re.search(r"<title>[^<]*>\s*([^<]+?)產業鏈簡介", page)
    cats = dict(re.findall(r'id="ic_link_(\w+)" class="company-chain-panel">([^<]+)<', page))
    rows = []
    for m in re.finditer(r'id="([^"]*(' + ic[:2] + r'[0-9A-F]{2})[^"]*)"', page):
        cid, key = m.group(1), m.group(2)
        if cid.startswith("ic_link_") or key not in cats:
            continue
        nxt = re.search(r'id="[^"]*' + ic[:2] + r'[0-9A-F]{2}[^"]*"', page[m.end():])
        seg = page[m.end(): m.end() + (nxt.start() if nxt else 20000)]
        for code, nm in re.findall(r"stk_code=(\d{4})[^>]*>([^<]*)", seg):
            rows.append((cats[key], code, html.unescape(nm).strip()))
    return (name.group(1).strip() if name else ic), list(dict.fromkeys(rows))


def main() -> int:
    if "--list" in sys.argv:
        for ic, nm in list_chains():
            print(ic, nm)
        return 0
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    ic = sys.argv[1]
    name, rows = chain_members(ic)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"產業鏈_{ic}_{name}_{date.today():%Y%m%d}.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["細項", "代號", "名稱"])
        w.writerows(rows)
    codes = {c for _, c, _ in rows}
    print(f"{name} ({ic})：{len(set(r[0] for r in rows))} 個細項、{len(codes)} 家公司 → {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
