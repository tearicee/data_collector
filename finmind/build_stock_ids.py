#!/usr/bin/env python3
"""
產生「逐股票下載用的個股代號」清單檔，供 SPONSOR 降級後 download_tick_by_stock.py
逐股票抓 PriceTick / KBar 使用 (bulk 失效後須逐 data_id 查)。

來源 (取聯集)：
  1. TaiwanStockInfo (台股總覽 master)：權威清單，實測完整涵蓋 PriceTick/KBar 有料股。
  2. 近期 bulk PriceTick / KBar 檔實際出現的 stock_id (預設掃最近 --recent 60 個交易日檔)：
     取得「實際會交易」的精確 tradeable 集，逐股票抓時可少打無料股的浪費請求。

輸出：finmind/stock_ids.csv 及 .parquet
  欄位：stock_id, stock_name, industry_category, type, in_info, in_pricetick, in_kbar

用法：python build_stock_ids.py [--recent 60]   (降級前先跑一次；可重跑刷新)
"""
import argparse
import glob
from datetime import datetime
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
OUT_ROOT = Path("/mnt/d/finmind_data")
INFO = OUT_ROOT / "TaiwanStockInfo" / "TaiwanStockInfo.parquet"
OUT_CSV = BASE_DIR / "stock_ids.csv"
OUT_PARQUET = BASE_DIR / "stock_ids.parquet"


def log(msg):
    print(f"{datetime.now():%H:%M:%S}  {msg}", flush=True)


def scan_recent(dataset, recent):
    files = sorted(glob.glob(str(OUT_ROOT / dataset / "*" / "*.parquet")))[-recent:]
    codes = set()
    log(f"[{dataset}] 掃描最近 {len(files)} 個交易日檔...")
    for f in files:
        try:
            codes |= set(pd.read_parquet(f, columns=["stock_id"])["stock_id"].dropna().astype(str).unique())
        except Exception as e:
            log(f"  跳過 {f} ({e})")
    log(f"[{dataset}] observed {len(codes)} 檔")
    return codes


def main():
    ap = argparse.ArgumentParser(description="產生逐股票下載用個股代號清單")
    ap.add_argument("--recent", type=int, default=60, help="掃描最近幾個交易日的 bulk 檔取活躍股 (預設 60)")
    args = ap.parse_args()

    info_map = {}
    info_codes = set()
    if INFO.exists():
        df = pd.read_parquet(INFO)
        df["stock_id"] = df["stock_id"].astype(str)
        info_codes = set(df["stock_id"])
        # 每檔取一列基本資料 (同 stock_id 可能多 type，取第一筆)
        info_map = df.drop_duplicates("stock_id").set_index("stock_id")[
            ["stock_name", "industry_category", "type"]].to_dict("index")
        log(f"TaiwanStockInfo：{len(info_codes)} 檔")
    else:
        log("WARNING: 找不到 TaiwanStockInfo master")

    ptk = scan_recent("TaiwanStockPriceTick", args.recent)
    kb = scan_recent("TaiwanStockKBar", args.recent)

    allcodes = sorted(info_codes | ptk | kb)
    rows = []
    for sid in allcodes:
        m = info_map.get(sid, {})
        rows.append(dict(
            stock_id=sid,
            stock_name=m.get("stock_name", ""),
            industry_category=m.get("industry_category", ""),
            type=m.get("type", ""),
            in_info=sid in info_codes,
            in_pricetick=sid in ptk,
            in_kbar=sid in kb,
        ))
    out = pd.DataFrame(rows).sort_values("stock_id").reset_index(drop=True)
    out.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    out.to_parquet(OUT_PARQUET, index=False)
    log(f"==== 完成：{len(out)} 個個股代號 → {OUT_CSV.name} / {OUT_PARQUET.name} ====")
    log(f"  TaiwanStockInfo={len(info_codes)}  PriceTick observed={len(ptk)}  KBar observed={len(kb)}  "
        f"聯集={len(out)}  (不在 Info={int((~out['in_info']).sum())})")


if __name__ == "__main__":
    main()
