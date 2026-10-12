#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
內部人持股轉讓事前申報 (申報轉讓) 每日抓取 → /mnt/d/mops/insider_transfer/申報轉讓_YYYY-MM.parquet (YYYY-MM = 出表日期月)
================================================================
  來源 (開放資料，各只給「最新一個出表日」的快照，所以要每天抓、累積)：
    上市 https://openapi.twse.com.tw/v1/opendata/t187ap12_L
    上櫃 https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap12_O
  (MOPS 新站的 t56sb12 查詢在舊站回「無持股轉讓之情形」，無法回補歷史；興櫃端點 302，暫不抓。)
  欄位：出表日期、公司代號、公司名稱、市場別、申報人身分、姓名、轉讓方式、轉讓股數(預定轉讓總股數-自有持股)、
        每日盤中最大得轉讓股數、受讓人、目前持股、轉讓後持股、有效轉讓期間、轉讓占持股_pct、first_seen。
  鍵：出表日期|公司代號|姓名|轉讓方式|轉讓股數 (同一人同日可能有多筆不同方式)。
  使用者 2026-10-12：轉讓方式為「一般交易」的申報一定要看到 → 新鮮度流程讀此檔產生 source=申報轉讓 事件。
用法：python material_info/download_insider_transfer.py [--dry]；cron 每天 4 次 (09:05、12:35、17:35、21:35)
"""
import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import heartbeat  # noqa: E402

OUT_DIR = Path("/mnt/d/mops/insider_transfer")
SRC = {"上市": "https://openapi.twse.com.tw/v1/opendata/t187ap12_L",
       "上櫃": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap12_O"}
HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36", "Accept": "application/json"}
COLS = ["出表日期", "公司代號", "公司名稱", "市場別", "申報人身分", "姓名", "轉讓方式", "轉讓股數", "每日盤中最大得轉讓股數", "受讓人",
        "目前持股", "轉讓後持股", "有效轉讓期間", "轉讓占持股_pct", "first_seen"]


def roc(s: str) -> str:
    s = str(s or "").strip()
    return f"{int(s[:-4]) + 1911}-{s[-4:-2]}-{s[-2:]}" if s[:-4].isdigit() and len(s) >= 7 else s


def num(v):
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def fetch(market: str) -> pd.DataFrame:
    r = requests.get(SRC[market], headers=HDR, timeout=60)
    r.raise_for_status()
    rows = []
    for x in r.json():
        g = lambda *ks: next((x[k] for k in ks if k in x and x[k] not in (None, "")), "")  # noqa: E731
        held = num(g("目前持有股數-自有持股"))
        shares = num(g("預定轉讓總股數-自有持股"))
        rows.append({
            "出表日期": roc(g("出表日期", "Date")), "公司代號": str(g("公司代號", "SecuritiesCompanyCode")).strip(),
            "公司名稱": str(g("公司名稱", "CompanyName")).strip(), "市場別": market,
            "申報人身分": str(g("申報人身分", "申請人身分")).strip(), "姓名": str(g("姓名")).strip(),
            "轉讓方式": str(g("預定轉讓方式及股數-轉讓方式")).strip(), "轉讓股數": shares,
            "每日盤中最大得轉讓股數": num(g("每日於盤中交易最大得轉讓股數")), "受讓人": str(g("受讓人")).strip(),
            "目前持股": held, "轉讓後持股": num(g("預定轉讓後持股-自有持股")),
            "有效轉讓期間": str(g("有效轉讓期間")).strip(),
            "轉讓占持股_pct": round(shares / held * 100, 1) if held > 0 else None,
        })
    return pd.DataFrame(rows, columns=COLS[:-1])


def key(df: pd.DataFrame) -> pd.Series:
    return df["出表日期"] + "|" + df["公司代號"] + "|" + df["姓名"] + "|" + df["轉讓方式"] + "|" + df["轉讓股數"].astype(int).astype(str)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    parts, errors = [], []
    for mk in SRC:
        try:
            parts.append(fetch(mk))
        except Exception as e:  # noqa: BLE001
            errors.append(mk)
            print(f"[WARN] {mk}: {e}")
        time.sleep(2)
    if not parts:
        heartbeat.write("insider_transfer", heartbeat.STATUS_FAIL, 1, "兩個來源都失敗")
        return 1
    new = pd.concat(parts, ignore_index=True)
    new = new[new["公司代號"].str.fullmatch(r"\d{4,6}") & (new["出表日期"] != "")]
    today = now[:10]   # 補抓到前幾天的出表 (假日後) 以出表日 18:00 當首次出現，不會被當成新事件推播
    new["first_seen"] = [now if d >= today else f"{d} 18:00:00" for d in new["出表日期"]]
    added = 0
    for ym, g in new.groupby(new["出表日期"].str[:7]):
        path = OUT_DIR / f"申報轉讓_{ym}.parquet"
        old = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=COLS)
        fresh = g[~key(g).isin(set(key(old)))] if len(old) else g
        for r in fresh.itertuples():
            print(f"{now} 新申報 {r.出表日期} {r.市場別} {r.公司代號} {r.公司名稱} {r.申報人身分} {r.姓名} {r.轉讓方式} {r.轉讓股數 / 1e3:,.0f} 張 (占持股 {r.轉讓占持股_pct}%)")
        if len(fresh) and not a.dry:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            out = pd.concat([old, fresh], ignore_index=True)
            tmp = path.with_suffix(".parquet.tmp")
            out.to_parquet(tmp, compression="zstd", index=False)
            tmp.replace(path)
        added += len(fresh)
    print(f"{now} 快照 {len(new)} 筆，新增 {added}")
    ok = not errors
    heartbeat.write("insider_transfer", heartbeat.STATUS_OK if ok else heartbeat.STATUS_FAIL, 0 if ok else 1,
                    f"新增 {added}；失敗 {errors}", stats={"new": added})
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
