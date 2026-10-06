#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每月營收彙總表輪詢 (MOPS) → /mnt/d/mops/month_revenue/月營收_YYYY-MM.parquet (YYYY-MM = 營收所屬月)
================================================================
  來源：公開資訊觀測站「每月營收」(t21sc04_ifrs)。新版查詢只是轉到舊站的靜態彙總頁：
    https://mopsov.twse.com.tw/nas/t21/{sii|otc|rotc}/t21sc03_{民國年}_{月}_0.html   (0=國內公司)
  公司一公告，彙總頁就會多一列 (頁面 Last-Modified 會更新)，所以用輪詢 + 比對來抓「新公告的營收」，
  每檔記 首次出現時間 (first_seen)，新鮮度評分把它當成一則事件 (source=月營收)。
  金額單位：仟元。欄位：公司代號、公司名稱、市場別、營收月、當月營收、上月營收、去年當月營收、月增_pct、年增_pct、
  累計營收、去年累計營收、累計年增_pct、備註、first_seen、頁面更新時間。
  輪詢對象：上個月的營收 (每月 1~10 日為公告期；10 日後仍會有遲交)。每次 3 個請求，cron 每 30 分鐘。
用法：python material_info/download_month_revenue.py [--ym 2026-09] [--dry]
"""
import argparse
import io
import sys
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import download_material_info as dmi  # noqa: E402  (共用 SESSION / headers)
from common import heartbeat  # noqa: E402

OUT_DIR = Path("/mnt/d/mops/month_revenue")
URL = "https://mopsov.twse.com.tw/nas/t21/{mk}/t21sc03_{y}_{m}_0.html"
MARKETS = {"sii": "上市", "otc": "上櫃", "rotc": "興櫃"}
COLS = ["公司代號", "公司名稱", "當月營收", "上月營收", "去年當月營收", "月增_pct", "年增_pct", "累計營收", "去年累計營收", "累計年增_pct", "備註"]


def fetch(mk: str, ym: str) -> tuple:
    y, m = int(ym[:4]) - 1911, int(ym[5:7])
    r = dmi.SESSION.get(URL.format(mk=mk, y=y, m=m), timeout=60)
    if r.status_code == 404:
        return pd.DataFrame(columns=COLS), ""
    r.raise_for_status()
    enc = "utf-8" if b"utf-8" in r.content[:600].lower() else "cp950"   # 舊站靜態頁是 Big5
    html = r.content.decode(enc, "ignore")
    mod = r.headers.get("Last-Modified", "")
    mod = parsedate_to_datetime(mod).astimezone().strftime("%Y-%m-%d %H:%M:%S") if mod else ""
    tabs = [t for t in pd.read_html(io.StringIO(html)) if t.shape[1] == 11 and t.shape[0] >= 1]
    if not tabs:
        return pd.DataFrame(columns=COLS), mod
    df = pd.concat(tabs, ignore_index=True)
    df.columns = COLS
    df = df[df["公司代號"].astype(str).str.fullmatch(r"\d{4,6}")].copy()
    for c in COLS[2:10]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["公司代號"] = df["公司代號"].astype(str)
    df["公司名稱"] = df["公司名稱"].astype(str).str.strip()
    df["備註"] = df["備註"].fillna("").astype(str).replace("-", "")
    df["市場別"] = MARKETS[mk]
    return df, mod


def update(ym: str, dry: bool = False) -> int:
    path = OUT_DIR / f"月營收_{ym}.parquet"
    old = pd.read_parquet(path) if path.exists() else pd.DataFrame()
    seen = dict(zip(old["公司代號"], old["first_seen"])) if len(old) else {}
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    parts = []
    for mk in MARKETS:
        df, mod = fetch(mk, ym)
        if len(df):
            df["營收月"] = ym
            df["頁面更新時間"] = mod
            # 首次建檔時沒有「公告時間」可用，以頁面更新時間當作 first_seen (都算舊的，不會被當成新事件推播)
            df["first_seen"] = [seen.get(c) or (now if seen else (mod or now)) for c in df["公司代號"]]
            parts.append(df)
        time.sleep(2)
    if not parts:
        return 0
    new = pd.concat(parts, ignore_index=True)
    fresh = new[~new["公司代號"].isin(seen)]
    for r in fresh.itertuples():
        print(f"{now} 新公告 {r.市場別} {r.公司代號} {r.公司名稱} {ym[5:]}月營收 {r.當月營收 / 1e5:,.2f} 億 月增 {r.月增_pct:+.1f}% 年增 {r.年增_pct:+.1f}%")
    if not dry:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        new.to_parquet(tmp, compression="zstd", index=False)
        tmp.replace(path)
    print(f"{now} {ym} 共 {len(new)} 家 (新增 {len(fresh)})")
    return len(fresh)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ym", help="營收所屬月 YYYY-MM (預設上個月)")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    ym = a.ym or (pd.Timestamp.now().to_period("M") - 1).strftime("%Y-%m")
    try:
        n = update(ym, a.dry)
        heartbeat.write("month_revenue", heartbeat.STATUS_OK, 0, f"{ym} 新增 {n}", stats={"new": n})
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] {e}")
        heartbeat.write("month_revenue", heartbeat.STATUS_FAIL, 1, str(e)[:200])
        return 1


if __name__ == "__main__":
    sys.exit(main())
