#!/usr/bin/env python3
"""
產生「所有券商分點代碼」清單檔，供未來 (SPONSOR 降級後) 逐券商代碼下載分點/權證分點報表使用。

來源 (取聯集，因單一來源不完整)：
  1. TaiwanSecuritiesTraderInfo (券商資訊表 master)：FinMind 官方券商清單，但實測仍漏少數代碼。
  2. 既有 bulk 分點報表實際出現的 securities_trader_id：
       TaiwanStockTradingDailyReport (2021-06-30~)、TaiwanStockWarrantTradingDailyReport (2023-06-21~)
     逐檔只讀 securities_trader_id 欄做 union (含歷年曾出現、現已關閉的分點，確保完整)。

輸出：
  finmind/securities_trader_ids.csv  及  .parquet
  欄位：securities_trader_id, securities_trader(名稱，來自 master，缺則空), in_master,
        in_trading_daily_report, in_warrant_daily_report

用法：python build_trader_ids.py   (可重跑刷新；降級前先跑一次拿到完整歷史代碼)
"""
import glob
from datetime import datetime
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
OUT_ROOT = Path("/mnt/d/finmind_data")
MASTER = OUT_ROOT / "TaiwanSecuritiesTraderInfo" / "TaiwanSecuritiesTraderInfo.parquet"
OUT_CSV = BASE_DIR / "securities_trader_ids.csv"
OUT_PARQUET = BASE_DIR / "securities_trader_ids.parquet"
DATASETS = ["TaiwanStockTradingDailyReport", "TaiwanStockWarrantTradingDailyReport"]


def log(msg):
    print(f"{datetime.now():%H:%M:%S}  {msg}", flush=True)


def scan_codes(dataset):
    files = sorted(glob.glob(str(OUT_ROOT / dataset / "*" / "*.parquet")))
    codes = set()
    log(f"[{dataset}] 掃描 {len(files)} 檔 (只讀 securities_trader_id 欄)...")
    for i, f in enumerate(files, 1):
        try:
            s = pd.read_parquet(f, columns=["securities_trader_id"])["securities_trader_id"]
            codes |= set(s.dropna().astype(str).unique())
        except Exception as e:
            log(f"  跳過 {f} ({e})")
        if i % 200 == 0:
            log(f"  {dataset} {i}/{len(files)}  累計代碼 {len(codes)}")
    log(f"[{dataset}] 完成：observed {len(codes)} 個代碼")
    return codes


def main():
    # master
    name_map = {}
    master_codes = set()
    if MASTER.exists():
        m = pd.read_parquet(MASTER)
        m["securities_trader_id"] = m["securities_trader_id"].astype(str)
        master_codes = set(m["securities_trader_id"])
        name_map = dict(zip(m["securities_trader_id"], m["securities_trader"]))
        log(f"master 券商表：{len(master_codes)} 個代碼")
    else:
        log("WARNING: 找不到 TaiwanSecuritiesTraderInfo master")

    tdr = scan_codes("TaiwanStockTradingDailyReport")
    war = scan_codes("TaiwanStockWarrantTradingDailyReport")

    allcodes = sorted(master_codes | tdr | war)
    df = pd.DataFrame({"securities_trader_id": allcodes})
    df["securities_trader"] = df["securities_trader_id"].map(name_map).fillna("")
    df["in_master"] = df["securities_trader_id"].isin(master_codes)
    df["in_trading_daily_report"] = df["securities_trader_id"].isin(tdr)
    df["in_warrant_daily_report"] = df["securities_trader_id"].isin(war)
    df = df.sort_values("securities_trader_id").reset_index(drop=True)

    df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    df.to_parquet(OUT_PARQUET, index=False)
    log(f"==== 完成：{len(df)} 個券商代碼 → {OUT_CSV.name} / {OUT_PARQUET.name} ====")
    log(f"  master={len(master_codes)}  TradingDailyReport={len(tdr)}  Warrant={len(war)}  "
        f"聯集={len(df)}  (不在master={int((~df['in_master']).sum())})")


if __name__ == "__main__":
    main()
