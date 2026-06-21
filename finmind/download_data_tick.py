#!/usr/bin/env python3
"""
下載 FinMind /api/v4/data 端點的逐筆資料：
  - TaiwanFuturesTick (期貨交易明細表)
  - TaiwanOptionTick  (選擇權交易明細表)

模式：一次拿特定日期、所有商品 (不帶 data_id，鎖 start_date=end_date=該日)。
回傳 JSON 轉存為 parquet 以節省空間。

輸出：/mnt/d/finmind_data/<dataset>/<年>/<dataset>_<日期>.parquet
（此資料集「不」做 Google Drive 同步）

用法：
  # 預設：兩個資料集，從近期往回下載到 2019-01-01
  python download_data_tick.py

  # 指定資料集 / 日期區間 / 順序
  python download_data_tick.py --datasets TaiwanFuturesTick --start 2019-01-01 --end 2026-06-01 --reverse

注意：期貨/選擇權偶有週六補班交易日，故「不」跳過週六；僅週日固定無交易而跳過以省請求。
已存在檔案自動跳過 (可中斷續傳)。
"""
import argparse
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from dotenv import dotenv_values

BASE_DIR = Path(__file__).resolve().parent
URL = "https://api.finmindtrade.com/api/v4/data"
OUT_ROOT = Path("/mnt/d/finmind_data")
LOG_FILE = BASE_DIR / "download_data_tick.log"

ALL_DATASETS = ["TaiwanFuturesTick", "TaiwanOptionTick"]
DEFAULT_START = "2019-01-01"

REQUEST_DELAY = 0.5          # 每次成功請求後的禮貌間隔 (秒)
MIN_REQUEST_INTERVAL = 1.0   # 每次有發出網路請求後的最小間隔 (含 nodata/fail)，避免短時間爆量請求
TIMEOUT = (10, 600)          # (連線, 讀取) 逾時；逐筆資料單日可達 60MB+
MAX_RETRIES = 5
BACKOFF_BASE = 5
# 連續失敗熔斷：連續這麼多次 fail 就中止，避免系統性錯誤(壞 token/參數/被限流)持續打 API 觸發 IP 封鎖
MAX_CONSECUTIVE_FAILURES = 8
# 額度 (402) 暫停：撞 API 上限時暫停等配額重置，而非中止丟資料 (常與其他 backfill 同時跑)
QUOTA_PAUSE = 600
QUOTA_MAX_PAUSES = 24


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_token() -> str:
    env = dotenv_values(BASE_DIR / ".env")
    token = env.get("finmind_api_key", "").strip()
    if not token:
        log("ERROR: .env 中找不到 finmind_api_key")
        sys.exit(1)
    return token


def daterange(start: date, end: date, reverse: bool):
    days = []
    d = start
    while d <= end:
        if d.weekday() != 6:      # 跳過週日 (固定無交易)，保留週六 (可能補班)
            days.append(d)
        d += timedelta(days=1)
    if reverse:
        days.reverse()
    return days


def out_path(dataset: str, d: str) -> Path:
    return OUT_ROOT / dataset / d[:4] / f"{dataset}_{d}.parquet"


def download_one(session, token, dataset: str, d: str) -> str:
    """回傳 'ok' / 'skip' / 'nodata' / 'fail'"""
    dest = out_path(dataset, d)
    if dest.exists() and dest.stat().st_size > 0:
        return "skip"

    headers = {"Authorization": f"Bearer {token}"}
    params = {"dataset": dataset, "start_date": d, "end_date": d}

    quota_pauses = 0
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = session.get(URL, headers=headers, params=params, timeout=TIMEOUT)
        except requests.RequestException as e:
            if attempt > MAX_RETRIES:
                log(f"  [{dataset}] {d} 連線重試 {MAX_RETRIES} 次仍失敗 ({e})")
                return "fail"
            wait = BACKOFF_BASE * attempt
            log(f"  [{dataset}] {d} 連線錯誤 ({e})，{wait}s 後重試 ({attempt}/{MAX_RETRIES})")
            time.sleep(wait)
            continue

        code = resp.status_code

        # 額度達上限：暫停等配額重置 (不丟資料)
        if code == 402:
            quota_pauses += 1
            if quota_pauses > QUOTA_MAX_PAUSES:
                log(f"  [{dataset}] {d} 額度 402 已暫停 {QUOTA_MAX_PAUSES} 次仍未恢復，視為失敗")
                return "fail"
            log(f"  [{dataset}] {d} [額度] HTTP 402 達 API 上限，暫停 {QUOTA_PAUSE}s 等配額重置 "
                f"({quota_pauses}/{QUOTA_MAX_PAUSES})")
            time.sleep(QUOTA_PAUSE)
            attempt = 0
            continue

        if code == 200:
            try:
                payload = resp.json()
            except ValueError:
                log(f"  [{dataset}] {d} 回傳非 JSON，跳過")
                return "fail"
            if payload.get("status") != 200:
                msg = str(payload.get("msg", ""))
                # 額度類訊息 → 暫停等配額重置
                if "limit" in msg.lower() or "402" in msg:
                    quota_pauses += 1
                    if quota_pauses > QUOTA_MAX_PAUSES:
                        log(f"  [{dataset}] {d} 額度訊息已暫停 {QUOTA_MAX_PAUSES} 次仍未恢復，視為失敗")
                        return "fail"
                    log(f"  [{dataset}] {d} [額度] {msg}，暫停 {QUOTA_PAUSE}s 等配額重置 "
                        f"({quota_pauses}/{QUOTA_MAX_PAUSES})")
                    time.sleep(QUOTA_PAUSE)
                    attempt = 0
                    continue
                log(f"  [{dataset}] {d} API 回應非 200: {msg}")
                return "fail"

            rows = payload.get("data", [])
            if not rows:
                return "nodata"

            df = pd.DataFrame(rows)
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".parquet.tmp")
            df.to_parquet(tmp, index=False)
            tmp.replace(dest)
            log(f"  [{dataset}] {d} OK  rows={len(df):,} size={dest.stat().st_size/1e6:.1f}MB")
            return "ok"

        if code in (401, 403):
            log(f"  [{dataset}] {d} 權限錯誤 HTTP {code}: {resp.text[:200]}")
            log("  -> token 可能失效或無此資料權限。中止。")
            sys.exit(2)

        if code == 429 or code >= 500:
            if attempt > MAX_RETRIES:
                log(f"  [{dataset}] {d} HTTP {code} 重試 {MAX_RETRIES} 次仍失敗")
                return "fail"
            wait = BACKOFF_BASE * attempt
            log(f"  [{dataset}] {d} HTTP {code}，{wait}s 後重試 ({attempt}/{MAX_RETRIES})")
            time.sleep(wait)
            continue

        log(f"  [{dataset}] {d} 非預期 HTTP {code}: {resp.text[:200]}")
        return "fail"


def main():
    ap = argparse.ArgumentParser(description="下載 FinMind 期貨/選擇權逐筆資料 (整日所有商品)")
    ap.add_argument("--datasets", default=",".join(ALL_DATASETS),
                    help=f"逗號分隔，預設 {','.join(ALL_DATASETS)}")
    ap.add_argument("--start", default=DEFAULT_START, help="起始日期 (預設 2019-01-01)")
    ap.add_argument("--end", default=date.today().isoformat(), help="結束日期 (預設今天)")
    ap.add_argument("--reverse", action="store_true", help="從最近日期往回下載")
    args = ap.parse_args()

    datasets = [s.strip() for s in args.datasets.split(",") if s.strip()]
    s = datetime.strptime(args.start, "%Y-%m-%d").date()
    e = datetime.strptime(args.end, "%Y-%m-%d").date()
    days = daterange(s, e, args.reverse)

    token = load_token()
    log(f"==== 開始下載 {datasets} | {args.start}~{args.end} | "
        f"{'reverse' if args.reverse else 'forward'} | 每集 {len(days)} 天 ====")

    session = requests.Session()
    consecutive_failures = 0
    for dataset in datasets:
        counts = {"ok": 0, "skip": 0, "nodata": 0, "fail": 0}
        failed = []
        log(f"---- 資料集 {dataset} ----")
        for i, dd in enumerate(days, 1):
            d = dd.isoformat()
            status = download_one(session, token, dataset, d)
            counts[status] += 1

            # 連續失敗熔斷 (跨資料集累計)，避免系統性錯誤持續打 API
            if status == "fail":
                failed.append(d)
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log(f"==== 連續 {consecutive_failures} 次失敗，熔斷中止 (避免 IP 被封鎖) ====")
                    sys.exit(3)
            else:
                consecutive_failures = 0

            # 只要有實際發出網路請求 (非 skip)，保持最小間隔
            if status != "skip":
                time.sleep(REQUEST_DELAY if status == "ok" else MIN_REQUEST_INTERVAL)
            if i % 50 == 0:
                log(f"  [{dataset}] 進度 {i}/{len(days)}  "
                    f"新={counts['ok']} 跳過={counts['skip']} 無資料={counts['nodata']} 失敗={counts['fail']}")
        log(f"==== {dataset} 完成 ====  新={counts['ok']} 已存在={counts['skip']} "
            f"無資料={counts['nodata']} 失敗={counts['fail']}")
        if failed:
            log(f"  [{dataset}] 失敗日期: {', '.join(failed)}")


if __name__ == "__main__":
    main()
