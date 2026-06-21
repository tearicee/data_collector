#!/usr/bin/env python3
"""
下載 FinMind「台灣股價歷史逐筆資料表 TaiwanStockPriceTick」
模式：一次拿特定日期、所有股票的資料 (storage_objects 端點，回傳 parquet)

需要 FinMind sponsor 等級會員 token。token 放在同目錄 .env：
    finmind_api_key=<your token>

用法：
    # 單一日期
    python download_tick.py --date 2024-01-02

    # 日期區間 (含頭尾)，自動跳過假日/無資料日
    python download_tick.py --start 2024-01-01 --end 2024-03-31

    # 從檔案讀日期 (每行一個 YYYY-MM-DD)
    python download_tick.py --dates-file dates.txt

輸出：data/TaiwanStockPriceTick/<年>/TaiwanStockPriceTick_<日期>.parquet
已存在的檔案會自動跳過 (可中斷後續傳)。
"""
import argparse
import io
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from dotenv import dotenv_values

BASE_DIR = Path(__file__).resolve().parent
DATASET = "TaiwanStockPriceTick"
URL = "https://api.finmindtrade.com/api/v4/storage_objects"
# 預設輸出到 D 槽 (WSL 掛載點)；可用 --out-dir 覆寫
DEFAULT_OUT_DIR = Path("/mnt/d/finmind_data")
LOG_FILE = BASE_DIR / "download_tick.log"

# 由 main() 依 --out-dir 設定，預設 <out-dir>/TaiwanStockPriceTick
OUT_ROOT = DEFAULT_OUT_DIR / DATASET

# 每次請求之間的禮貌性間隔 (秒)，避免觸發 rate limit
REQUEST_DELAY = 1.0
# 每次「有發出網路請求」後的最小間隔 (含 nodata/fail)，避免短時間累積大量請求被 FinMind 自動封鎖
MIN_REQUEST_INTERVAL = 1.0
# 連線/讀取逾時 (秒)
TIMEOUT = (10, 300)
# 暫時性錯誤的重試次數與退避基數
MAX_RETRIES = 5
BACKOFF_BASE = 5
# 連續失敗熔斷：連續這麼多次 fail 就中止整個任務，避免系統性錯誤(壞 token/參數/被限流)持續打 API 觸發 IP 封鎖
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


def parse_blackout(s: str):
    """'08:00-14:30' → (start_minutes, end_minutes)；空字串表示不啟用。"""
    if not s:
        return None
    a, b = s.split("-")
    ah, am = map(int, a.split(":"))
    bh, bm = map(int, b.split(":"))
    return (ah * 60 + am, bh * 60 + bm)


def wait_if_blackout(blackout):
    """若目前在黑名單時段內 (盤中)，迴圈等待至時段結束 (每 60s 檢查)。"""
    if not blackout:
        return
    announced = False
    while True:
        now = datetime.now()
        cur = now.hour * 60 + now.minute
        if blackout[0] <= cur < blackout[1]:
            if not announced:
                log(f"  [黑名單] 進入盤中暫停時段，等待至 {blackout[1]//60:02d}:{blackout[1]%60:02d} 後續跑")
                announced = True
            time.sleep(60)
        else:
            if announced:
                log("  [黑名單] 時段結束，恢復下載")
            return


def daterange(start: date, end: date):
    d = start
    while d <= end:
        # 跳過週六(5)、週日(6)，減少不必要的請求
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def parse_dates(args) -> list[str]:
    if args.date:
        return [args.date]
    if args.dates_file:
        lines = Path(args.dates_file).read_text(encoding="utf-8").splitlines()
        return [ln.strip() for ln in lines if ln.strip()]
    if args.start and args.end:
        s = datetime.strptime(args.start, "%Y-%m-%d").date()
        e = datetime.strptime(args.end, "%Y-%m-%d").date()
        days = [d.isoformat() for d in daterange(s, e)]
        if args.reverse:
            days.reverse()  # 從最近日期開始往回下載
        return days
    log("ERROR: 請指定 --date 或 --start/--end 或 --dates-file")
    sys.exit(1)


def out_path(d: str) -> Path:
    year = d[:4]
    return OUT_ROOT / year / f"{DATASET}_{d}.parquet"


def download_one(session: requests.Session, token: str, d: str) -> str:
    """回傳狀態字串: 'ok' / 'skip' / 'nodata' / 'fail'"""
    dest = out_path(d)
    if dest.exists() and dest.stat().st_size > 0:
        return "skip"

    headers = {"Authorization": f"Bearer {token}"}
    params = {"dataset": DATASET, "date": d}

    quota_pauses = 0
    attempt = 0
    while True:
        attempt += 1
        try:
            # allow_redirects=True 會自動跟隨 307 到 presigned S3 URL
            resp = session.get(URL, headers=headers, params=params,
                               timeout=TIMEOUT, allow_redirects=True)
        except requests.RequestException as e:
            if attempt > MAX_RETRIES:
                log(f"  {d} 連線重試 {MAX_RETRIES} 次仍失敗 ({e})")
                return "fail"
            wait = BACKOFF_BASE * attempt
            log(f"  {d} 連線錯誤 ({e})，{wait}s 後重試 ({attempt}/{MAX_RETRIES})")
            time.sleep(wait)
            continue

        code = resp.status_code

        # 額度達上限：暫停等配額重置 (不丟資料)
        if code == 402:
            quota_pauses += 1
            if quota_pauses > QUOTA_MAX_PAUSES:
                log(f"  {d} 額度 402 已暫停 {QUOTA_MAX_PAUSES} 次仍未恢復，視為失敗")
                return "fail"
            log(f"  {d} [額度] HTTP 402 達 API 上限，暫停 {QUOTA_PAUSE}s 等配額重置 "
                f"({quota_pauses}/{QUOTA_MAX_PAUSES})")
            time.sleep(QUOTA_PAUSE)
            attempt = 0
            continue

        if code == 200:
            content = resp.content
            if content[:4] != b"PAR1":
                log(f"  {d} 回傳非 parquet 內容，跳過: {content[:200]!r}")
                return "fail"
            # 驗證可讀，再原子寫入
            try:
                import pandas as pd
                df = pd.read_parquet(io.BytesIO(content))
            except Exception as e:
                log(f"  {d} parquet 解析失敗: {e}")
                return "fail"
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".parquet.tmp")
            tmp.write_bytes(content)
            tmp.replace(dest)
            nstk = df["stock_id"].nunique() if "stock_id" in df.columns else len(df)
            log(f"  {d} OK  rows={len(df):,} stocks={nstk} size={len(content)/1e6:.1f}MB")
            return "ok"

        if code == 404:
            log(f"  {d} 無資料 (假日/未開盤/尚未發布)")
            return "nodata"

        if code in (401, 403):
            log(f"  {d} 權限錯誤 HTTP {code}: {resp.text[:200]}")
            log("  -> 此模式需要 FinMind sponsor 等級會員。中止。")
            sys.exit(2)

        if code == 429 or code >= 500:
            if attempt > MAX_RETRIES:
                log(f"  {d} HTTP {code} 重試 {MAX_RETRIES} 次仍失敗")
                return "fail"
            wait = BACKOFF_BASE * attempt
            log(f"  {d} HTTP {code} (rate limit/伺服器忙)，{wait}s 後重試 "
                f"({attempt}/{MAX_RETRIES})")
            time.sleep(wait)
            continue

        log(f"  {d} 非預期 HTTP {code}: {resp.text[:200]}")
        return "fail"


def main():
    global DATASET, OUT_ROOT
    ap = argparse.ArgumentParser(description="下載 FinMind storage_objects 整日資料 (預設 TaiwanStockPriceTick)")
    ap.add_argument("--date", help="單一日期 YYYY-MM-DD")
    ap.add_argument("--start", help="起始日期 YYYY-MM-DD")
    ap.add_argument("--end", help="結束日期 YYYY-MM-DD")
    ap.add_argument("--dates-file", help="日期清單檔 (每行一個 YYYY-MM-DD)")
    ap.add_argument("--reverse", action="store_true",
                    help="從最近日期開始往回下載 (僅 --start/--end 模式)")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR),
                    help=f"輸出根目錄 (預設 {DEFAULT_OUT_DIR})")
    ap.add_argument("--dataset", default=DATASET,
                    help=f"storage_objects 資料集 (預設 {DATASET}；亦支援 TaiwanStockKBar / TaiwanStockTradingDailyReport)")
    ap.add_argument("--blackout", default="", help="盤中暫停時段，如 08:00-14:30")
    args = ap.parse_args()

    DATASET = args.dataset
    OUT_ROOT = Path(args.out_dir) / DATASET
    blackout = parse_blackout(args.blackout)

    token = load_token()
    dates = parse_dates(args)
    log(f"==== 開始下載 {DATASET}，共 {len(dates)} 個交易日候選 ====")

    counts = {"ok": 0, "skip": 0, "nodata": 0, "fail": 0}
    session = requests.Session()
    failed_dates = []
    consecutive_failures = 0

    for i, d in enumerate(dates, 1):
        wait_if_blackout(blackout)
        log(f"[{i}/{len(dates)}] {d}")
        status = download_one(session, token, d)
        counts[status] += 1

        # 連續失敗熔斷：避免系統性錯誤持續打 API 觸發封鎖
        if status == "fail":
            failed_dates.append(d)
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log(f"==== 連續 {consecutive_failures} 次失敗，熔斷中止 (避免 IP 被封鎖) ====")
                log(f"已完成: 新={counts['ok']} 跳過={counts['skip']} 無資料={counts['nodata']} 失敗={counts['fail']}")
                sys.exit(3)
        else:
            consecutive_failures = 0

        # 只要有實際發出網路請求 (非 skip)，就保持最小間隔，避免短時間爆量請求
        if status != "skip":
            time.sleep(REQUEST_DELAY if status == "ok" else MIN_REQUEST_INTERVAL)

    log(f"==== 完成 ====  新下載={counts['ok']} 已存在={counts['skip']} "
        f"無資料={counts['nodata']} 失敗={counts['fail']}")
    if failed_dates:
        log(f"失敗日期: {', '.join(failed_dates)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
