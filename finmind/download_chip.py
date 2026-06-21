#!/usr/bin/env python3
"""
下載 FinMind「台股 - 籌碼面」資料集 (走 /api/v4/data 端點)。

取得模式 (見 REGISTRY)：
  - snapshot       : 整表快照 (無日期)，每次重抓覆寫單一檔 (證券商資訊表)
  - range_snapshot : 一次帶整段 start~today 取回整表，覆寫單一檔
                     (大盤融資維持率 / 處置證券 / 暫停融券賣出表 — 市場級或稀疏通知，整段一次抓最省請求)
  - daily          : 逐交易日、整個市場 (不帶 data_id，鎖 start=end=該日)，每日一檔

storage_objects 的「台股權證分點 TaiwanStockWarrantTradingDailyReport」不在此處，
改用 download_tick.py --dataset TaiwanStockWarrantTradingDailyReport (2024-01-02 起，更早無物件)。

排除：
  - TaiwanStockTradingDailyReport (一般分點)：已由 download_tick.py / daily_update.sh 維護 (重複)
  - TaiwanStockTradingDailyReportSecIdAgg (當日券商分點統計)：該端點需同時帶
    data_id(股票)+securities_trader_id(券商) 才能查，無「整日全市場」批次模式，不適合完整回補

API 額度保護 (因常與其他 backfill 同時跑)：
  遇 HTTP 402 / 額度訊息 → **暫停** QUOTA_PAUSE 秒後重試 (配額每小時重置)，最多暫停
  QUOTA_MAX_PAUSES 次；不會因撞上限而中止丟資料。401/403 (token 失效/無權限) 仍立即中止。
  另含：連續失敗熔斷、每請求最小間隔、原子寫檔 (tmp→replace) 可安全中斷續傳。

盤中讓道：--blackout 08:00-14:30 期間自動暫停 (不發請求)，時段外自動續跑。

用法：
  python download_chip.py --reverse --blackout 08:00-14:30          # 全部，從近期往回回補
  python download_chip.py --datasets TaiwanStockShareholding --start 2004-01-01 --reverse
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
LOG_FILE = BASE_DIR / "download_chip.log"

# 資料集登錄表：mode + 已知最早可用日期 (API 探測得出)
REGISTRY = {
    # 整表快照 (無日期)
    "TaiwanSecuritiesTraderInfo":                  {"mode": "snapshot"},
    # 整段一次抓 (市場級 / 稀疏通知)，覆寫單檔
    "TaiwanTotalExchangeMarginMaintenance":        {"mode": "range_snapshot", "start": "2001-01-01"},
    "TaiwanStockDispositionSecuritiesPeriod":      {"mode": "range_snapshot", "start": "2001-01-01"},
    "TaiwanStockMarginShortSaleSuspension":        {"mode": "range_snapshot", "start": "2015-01-01"},
    # 逐交易日、整個市場
    "TaiwanStockMarginPurchaseShortSale":          {"mode": "daily", "start": "2001-01-01"},
    "TaiwanStockInstitutionalInvestorsBuySell":    {"mode": "daily", "start": "2012-05-01"},
    "TaiwanStockInstitutionalInvestorsBuySellWide":{"mode": "daily", "start": "2012-05-01"},
    "TaiwanStockShareholding":                     {"mode": "daily", "start": "2004-02-01"},
    "TaiwanStockHoldingSharesPer":                 {"mode": "daily", "start": "2010-01-01"},  # 集保週資料，非當週回 nodata
    "TaiwanStockSecuritiesLending":                {"mode": "daily", "start": "2003-11-01"},
    "TaiwanDailyShortSaleBalances":                {"mode": "daily", "start": "2005-07-01"},
    "TaiwanStockLoanCollateralBalance":            {"mode": "daily", "start": "2006-10-01"},  # 限 sponsor
    "TaiwanStockDayTradingBorrowingFeeRate":       {"mode": "daily", "start": "2015-10-01"},
}
ALL_DATASETS = list(REGISTRY.keys())

REQUEST_DELAY = 0.5
MIN_REQUEST_INTERVAL = 1.0
TIMEOUT = (10, 600)
MAX_RETRIES = 5
BACKOFF_BASE = 5
MAX_CONSECUTIVE_FAILURES = 8
# 額度 (402) 暫停：每次等多久、最多等幾次 (10 分鐘 × 24 = 最長 4 小時，足以跨過每小時配額重置)
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
    if not s:
        return None
    a, b = s.split("-")
    ah, am = map(int, a.split(":"))
    bh, bm = map(int, b.split(":"))
    return (ah * 60 + am, bh * 60 + bm)


def wait_if_blackout(blackout):
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


def is_quota_msg(msg: str) -> bool:
    m = msg.lower()
    return "limit" in m or "402" in m or "request" in m and "exceed" in m


def api_get(session, token, params, blackout=None):
    """回傳 (rows, status)。status: 'ok'/'nodata'/'fail'。
    402/額度 → 暫停等待配額重置後重試 (不丟資料)；401/403 → 立即 sys.exit。"""
    headers = {"Authorization": f"Bearer {token}"}
    quota_pauses = 0
    attempt = 0
    while True:
        attempt += 1
        wait_if_blackout(blackout)
        try:
            resp = session.get(URL, headers=headers, params=params, timeout=TIMEOUT)
        except requests.RequestException as e:
            if attempt > MAX_RETRIES:
                log(f"    連線重試 {MAX_RETRIES} 次仍失敗 ({e})")
                return None, "fail"
            wait = BACKOFF_BASE * attempt
            log(f"    連線錯誤 ({e})，{wait}s 後重試 ({attempt}/{MAX_RETRIES})")
            time.sleep(wait)
            continue

        code = resp.status_code

        # --- 額度達上限：暫停等待重置 ---
        if code == 402:
            quota_pauses += 1
            if quota_pauses > QUOTA_MAX_PAUSES:
                log(f"    額度 402 已暫停 {QUOTA_MAX_PAUSES} 次仍未恢復，視為失敗")
                return None, "fail"
            log(f"    [額度] HTTP 402 達 API 上限，暫停 {QUOTA_PAUSE}s 等配額重置 "
                f"({quota_pauses}/{QUOTA_MAX_PAUSES})")
            time.sleep(QUOTA_PAUSE)
            attempt = 0
            continue

        if code == 200:
            try:
                payload = resp.json()
            except ValueError:
                log("    回傳非 JSON")
                return None, "fail"
            if payload.get("status") != 200:
                msg = str(payload.get("msg", ""))
                if is_quota_msg(msg):
                    quota_pauses += 1
                    if quota_pauses > QUOTA_MAX_PAUSES:
                        log(f"    額度訊息已暫停 {QUOTA_MAX_PAUSES} 次仍未恢復，視為失敗")
                        return None, "fail"
                    log(f"    [額度] {msg}，暫停 {QUOTA_PAUSE}s 等配額重置 "
                        f"({quota_pauses}/{QUOTA_MAX_PAUSES})")
                    time.sleep(QUOTA_PAUSE)
                    attempt = 0
                    continue
                log(f"    API 回應非 200: {msg}")
                return None, "fail"
            rows = payload.get("data", []) or []
            return rows, ("ok" if rows else "nodata")

        if code in (401, 403):
            log(f"    權限錯誤 HTTP {code}: {resp.text[:200]}")
            log("    -> token 失效或無此資料權限 (部分資料集限 sponsor)。中止。")
            sys.exit(2)

        if code == 429 or code >= 500:
            if attempt > MAX_RETRIES:
                log(f"    HTTP {code} 重試 {MAX_RETRIES} 次仍失敗")
                return None, "fail"
            wait = BACKOFF_BASE * attempt
            log(f"    HTTP {code}，{wait}s 後重試 ({attempt}/{MAX_RETRIES})")
            time.sleep(wait)
            continue

        log(f"    非預期 HTTP {code}: {resp.text[:200]}")
        return None, "fail"


def save_parquet(rows, dest: Path):
    df = pd.DataFrame(rows)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(dest)
    return df


def out_path_dated(dataset: str, d: str) -> Path:
    return OUT_ROOT / dataset / d[:4] / f"{dataset}_{d}.parquet"


def out_path_snapshot(dataset: str) -> Path:
    return OUT_ROOT / dataset / f"{dataset}.parquet"


def daily_dates(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def download_snapshot(session, token, dataset, params_extra=None, blackout=None):
    params = {"dataset": dataset}
    if params_extra:
        params.update(params_extra)
    rows, status = api_get(session, token, params, blackout)
    if status == "ok":
        dest = out_path_snapshot(dataset)
        df = save_parquet(rows, dest)
        log(f"  [{dataset}] 快照 OK  rows={len(df):,} size={dest.stat().st_size/1e6:.1f}MB -> {dest.name}")
    elif status == "nodata":
        log(f"  [{dataset}] 快照無資料")
    else:
        log(f"  [{dataset}] 快照失敗")
    return status


def download_dated(session, token, dataset, d: str, blackout=None) -> str:
    dest = out_path_dated(dataset, d)
    if dest.exists() and dest.stat().st_size > 0:
        return "skip"
    rows, status = api_get(session, token, {"dataset": dataset, "start_date": d, "end_date": d}, blackout)
    if status == "ok":
        df = save_parquet(rows, dest)
        log(f"  [{dataset}] {d} OK  rows={len(df):,} size={dest.stat().st_size/1e6:.1f}MB")
    return status


def main():
    ap = argparse.ArgumentParser(description="下載 FinMind 籌碼面 (/data) 資料集")
    ap.add_argument("--datasets", default=",".join(ALL_DATASETS),
                    help="逗號分隔；預設全部籌碼面 (/data) 資料集")
    ap.add_argument("--start", help="覆寫起始日期 (預設用各資料集已知最早日)")
    ap.add_argument("--end", default=date.today().isoformat(), help="結束日期 (預設今天)")
    ap.add_argument("--reverse", action="store_true", help="逐日資料從最近往回下載")
    ap.add_argument("--blackout", default="", help="盤中暫停時段，如 08:00-14:30")
    args = ap.parse_args()

    datasets = [s.strip() for s in args.datasets.split(",") if s.strip()]
    unknown = [d for d in datasets if d not in REGISTRY]
    if unknown:
        log(f"ERROR: 未知資料集 {unknown}，可用: {list(REGISTRY)}")
        sys.exit(1)

    blackout = parse_blackout(args.blackout)
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    token = load_token()
    session = requests.Session()
    consecutive_failures = 0

    log(f"==== 開始下載籌碼面資料集 {datasets} | end={args.end} | "
        f"{'reverse' if args.reverse else 'forward'} | blackout={args.blackout or '無'} ====")

    for dataset in datasets:
        spec = REGISTRY[dataset]
        mode = spec["mode"]
        wait_if_blackout(blackout)

        if mode == "snapshot":
            download_snapshot(session, token, dataset, blackout=blackout)
            time.sleep(MIN_REQUEST_INTERVAL)
            continue

        if mode == "range_snapshot":
            start = args.start or spec["start"]
            download_snapshot(session, token, dataset,
                              {"start_date": start, "end_date": args.end}, blackout=blackout)
            time.sleep(MIN_REQUEST_INTERVAL)
            continue

        # daily
        start = datetime.strptime(args.start or spec["start"], "%Y-%m-%d").date()
        days = list(daily_dates(start, end))
        if args.reverse:
            days.reverse()
        counts = {"ok": 0, "skip": 0, "nodata": 0, "fail": 0}
        failed = []
        log(f"---- {dataset} (daily) 共 {len(days)} 個候選日 ----")
        for i, dd in enumerate(days, 1):
            d = dd.isoformat()
            status = download_dated(session, token, dataset, d, blackout)
            counts[status] += 1
            if status == "fail":
                failed.append(d)
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log(f"==== 連續 {consecutive_failures} 次失敗，熔斷中止 (避免 IP 被封鎖) ====")
                    sys.exit(3)
            else:
                consecutive_failures = 0
            if status != "skip":
                time.sleep(REQUEST_DELAY if status == "ok" else MIN_REQUEST_INTERVAL)
            if i % 100 == 0:
                log(f"  [{dataset}] 進度 {i}/{len(days)}  "
                    f"新={counts['ok']} 跳過={counts['skip']} 無資料={counts['nodata']} 失敗={counts['fail']}")
        log(f"==== {dataset} 完成 ====  新={counts['ok']} 已存在={counts['skip']} "
            f"無資料={counts['nodata']} 失敗={counts['fail']}")
        if failed:
            log(f"  [{dataset}] 失敗日期: {', '.join(failed[:30])}{' ...' if len(failed)>30 else ''}")

    log("==== 籌碼面下載流程結束 ====")


if __name__ == "__main__":
    main()
