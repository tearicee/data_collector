#!/usr/bin/env python3
"""
每日更新 FinMind「股利」資料 (sponsor 專屬：單一日期一次取全市場)。

背景：
  TaiwanStockDividend / TaiwanStockDividendResult 在 download_fundamental.py 走 per_stock
  模式 (逐股票、必帶 data_id)，一旦有檔即 skip-existing，不會自動補進新公告的股利。
  若用 --refresh 逐股票全量重抓需 ~3000 請求/集，負擔大。

  實測 sponsor 會員可「只給單一 start_date (不帶 data_id)」一次取回該『除息日』全市場資料，
  欄位與逐股票完全一致 (22 欄)、且該日股票集合與內容完全相同 → 完整且權威。
  (注意：給「日期區間」會回殘缺資料，只有「單一日期」才完整。)

策略：
  每天只掃「近期回看 + 未來數月」的每一天 (含週末，因股利 date 欄可能落在週末)，每日一個請求取全市場，
  再把結果 merge 進既有 by_stock 檔：該日期窗一律以 API 為準 (可同時納入新公告與事後修訂)，
  窗外的歷史原樣保留。如此每天僅約一兩百個請求即可維持股利資料最新。

用法：
  python update_dividend.py                                   # 每日：近 --back 天 ~ 未來 --forward 天
  python update_dividend.py --start 2026-06-01 --end 2026-12-31   # 一次性補抓 (覆寫該窗)
  python update_dividend.py --datasets TaiwanStockDividend --blackout 08:00-14:30
"""
import argparse
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

# 複用 download_fundamental 的 API 存取 (含 402 配額暫停、盤中讓道、401/403 立即中止)
from download_fundamental import (
    api_get, load_token, parse_blackout, wait_if_blackout,
    out_path_stock, OUT_ROOT, MIN_REQUEST_INTERVAL, REQUEST_DELAY,
)

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "update_dividend.log"

DATASETS = ["TaiwanStockDividend", "TaiwanStockDividendResult", "TaiwanStockMarketValueWeight"]
# 各集 merge 去重鍵 (不同集欄名不同；同 key 保留最新抓到的)
DEDUP_SUBSETS = {
    "TaiwanStockDividend":           ["date", "stock_id", "year"],
    "TaiwanStockDividendResult":     ["date", "stock_id"],
    "TaiwanStockMarketValueWeight":  ["date", "stock_id"],   # 無 year 欄，月底發布
}
# 各集日期視窗覆寫 (back, forward)；未列者用 CLI 預設。
#   股利：除息日在未來 → 需大 forward 才抓得到新公告；
#   市值比重：月底發布、無未來資料 → 只回看即可，forward 設小省請求。
WINDOW = {
    "TaiwanStockMarketValueWeight": (45, 3),
}


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def save_df_atomic(df: pd.DataFrame, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(dest)


def window_dates(start: date, end: date):
    # ⚠️ 不可跳週末！TaiwanStockDividend 的 date 欄(配息/股利基準日)可能落在週末
    #    (例：9910 豐泰 2026 配息 date=2026-06-21 為週日)，跳週末會整批漏抓。
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def fetch_window(session, token, dataset, start: date, end: date, blackout) -> pd.DataFrame:
    """逐日單請求取全市場，彙整成一個 DataFrame (該窗全部股票的記錄)。"""
    days = list(window_dates(start, end))
    frames = []
    counts = {"ok": 0, "nodata": 0, "fail": 0}
    log(f"---- {dataset}：掃描 {start} ~ {end} 共 {len(days)} 天 ----")
    for i, dd in enumerate(days, 1):
        d = dd.isoformat()
        rows, status = api_get(session, token, {"dataset": dataset, "start_date": d, "end_date": d}, blackout)
        counts[status if status in counts else "fail"] += 1
        if status == "ok":
            frames.append(pd.DataFrame(rows))
        time.sleep(REQUEST_DELAY if status == "ok" else MIN_REQUEST_INTERVAL)
        if i % 50 == 0:
            log(f"  [{dataset}] {i}/{len(days)}  有料日={counts['ok']} 無料={counts['nodata']} 失敗={counts['fail']}")
    log(f"==== {dataset} 掃描完成 ==== 有料日={counts['ok']} 無料={counts['nodata']} 失敗={counts['fail']}")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def merge_into_by_stock(dataset, win: pd.DataFrame, start: date, end: date) -> dict:
    """把窗內資料併回 by_stock：窗內 [start,end] 以 API 為準，窗外歷史保留。"""
    s, e = start.isoformat(), end.isoformat()
    subset = [c for c in DEDUP_SUBSETS.get(dataset, ["date", "stock_id"]) if c in win.columns]
    stats = {"updated": 0, "created": 0, "unchanged": 0}
    if win.empty:
        log(f"  [{dataset}] 窗內無任何資料，無需 merge")
        return stats
    for sid, new_rows in win.groupby(win["stock_id"].astype(str)):
        dest = out_path_stock(dataset, sid)
        new_rows = new_rows.copy()
        if dest.exists() and dest.stat().st_size > 0:
            old = pd.read_parquet(dest)
            old_dates = old["date"].astype(str)
            keep = old[(old_dates < s) | (old_dates > e)]          # 保留窗外歷史
            merged = pd.concat([keep, new_rows], ignore_index=True)
            if subset:
                merged = merged.drop_duplicates(subset=subset, keep="last")
            merged = merged.sort_values("date").reset_index(drop=True)
            # 與舊檔比對是否真有變化 (避免無謂寫檔)
            if len(merged) == len(old) and merged.sort_index(axis=1).reset_index(drop=True).equals(
                    old.sort_values("date").reset_index(drop=True).sort_index(axis=1)):
                stats["unchanged"] += 1
                continue
            save_df_atomic(merged, dest)
            stats["updated"] += 1
        else:
            merged = new_rows.drop_duplicates(subset=subset, keep="last") if subset else new_rows
            merged = merged.sort_values("date").reset_index(drop=True)
            save_df_atomic(merged, dest)
            stats["created"] += 1
    log(f"  [{dataset}] merge 完成：更新={stats['updated']} 新建={stats['created']} 無變動={stats['unchanged']}")
    return stats


def main():
    ap = argparse.ArgumentParser(description="每日更新股利資料 (sponsor 單日全市場端點)")
    ap.add_argument("--datasets", default=",".join(DATASETS), help="逗號分隔；預設 Dividend + DividendResult")
    ap.add_argument("--back", type=int, default=10, help="回看天數 (補抓延遲發布/修訂)；預設 10")
    ap.add_argument("--forward", type=int, default=210, help="前看天數 (涵蓋未來除息日)；預設 210")
    ap.add_argument("--start", help="明確起始日 (覆寫 --back，用於一次性補抓)")
    ap.add_argument("--end", help="明確結束日 (覆寫 --forward)")
    ap.add_argument("--blackout", default="", help="盤中暫停時段，如 08:00-14:30")
    args = ap.parse_args()

    datasets = [x.strip() for x in args.datasets.split(",") if x.strip()]
    today = date.today()
    explicit = bool(args.start or args.end)   # 明確指定日期 → 套用到所有集 (一次性補抓)
    blackout = parse_blackout(args.blackout)

    token = load_token()
    session = requests.Session()
    log(f"==== 股利/市值比重更新開始 {datasets} | blackout={args.blackout or '無'} ====")
    for dataset in datasets:
        if explicit:
            start = datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else today - timedelta(days=args.back)
            end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else today + timedelta(days=args.forward)
        else:
            back, forward = WINDOW.get(dataset, (args.back, args.forward))
            start = today - timedelta(days=back)
            end = today + timedelta(days=forward)
        wait_if_blackout(blackout)
        log(f"-- {dataset} 視窗 {start} ~ {end} --")
        win = fetch_window(session, token, dataset, start, end, blackout)
        merge_into_by_stock(dataset, win, start, end)
        time.sleep(MIN_REQUEST_INTERVAL)
    log("==== 更新流程結束 ====")


if __name__ == "__main__":
    main()
