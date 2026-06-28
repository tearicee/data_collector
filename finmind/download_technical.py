#!/usr/bin/env python3
"""
下載 FinMind「台股 - 技術面」資料集 (走 /api/v4/data 端點)。

支援多種取得模式 (見 REGISTRY)：
  - snapshot       : 整表快照，無日期參數，每次重抓覆寫單一檔 (代碼/交易日/權證對照表)
  - range_snapshot : 一次帶整段 start~today 取回，覆寫單一檔 (當沖預告表，量小且為前瞻通知)
  - daily          : 逐交易日、整個市場 (不帶 data_id，鎖 start=end=該日)，每日一檔
  - weekly         : 逐「週一」整個市場 (週K 的 date 慣例為當週週一)，每週一一檔
  - monthly        : 逐「每月 1 號」整個市場 (月K 的 date 慣例為當月 1 號)，每月一檔

storage_objects 端點的資料集 (TaiwanStockPriceTick / TaiwanStockKBar) 不在此處，
改用 download_tick.py --dataset <name> 下載。

輸出：
  per-date  → /mnt/d/finmind_data/<dataset>/<年>/<dataset>_<date>.parquet
  snapshot  → /mnt/d/finmind_data/<dataset>/<dataset>.parquet  (覆寫)

防封鎖 (沿用 download_tick.py / download_data_tick.py 機制)：
  連續失敗熔斷 + 每請求最小間隔 + 401/402/403 立即中止。
黑名單時段：--blackout 08:00-14:30 期間自動暫停 (台股盤中讓出網路頻寬)，時段外自動續跑。

用法：
  # 全部技術面 (/data) 資料集，從近期往回回補，盤中暫停
  python download_technical.py --reverse --blackout 08:00-14:30

  # 指定資料集 / 日期區間
  python download_technical.py --datasets TaiwanStockPrice,TaiwanStockPriceAdj \
      --start 2000-01-01 --end 2026-06-15 --reverse

  # 只刷新整表快照
  python download_technical.py --datasets TaiwanStockInfo,TaiwanStockTradingDate
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
LOG_FILE = BASE_DIR / "download_technical.log"

# 資料集登錄表：mode 決定取得方式，start 為已知最早可用日期 (探測 API 得出)
REGISTRY = {
    # 整表快照 (無日期)
    "TaiwanStockInfo":                    {"mode": "snapshot"},
    "TaiwanStockInfoWithWarrant":         {"mode": "snapshot"},
    "TaiwanStockInfoWithWarrantSummary":  {"mode": "snapshot"},   # 只限 sponsor
    "TaiwanStockTradingDate":             {"mode": "snapshot"},
    # 前瞻通知表 (一次取整段，量小)
    "TaiwanStockDayTradingSuspension":    {"mode": "range_snapshot", "start": "2014-01-01"},  # backer/sponsor
    # 逐交易日、整個市場
    "TaiwanStockPrice":                   {"mode": "daily",   "start": "2000-01-01"},
    "TaiwanStockPriceAdj":                {"mode": "daily",   "start": "2000-01-01"},
    "TaiwanStockDayTrading":              {"mode": "daily",   "start": "2015-01-01"},
    "TaiwanStockPriceLimit":              {"mode": "daily",   "start": "2010-01-01"},
    "TaiwanVariousIndicators5Seconds":    {"mode": "daily",   "start": "2010-01-01"},
    "TaiwanStockEvery5SecondsIndex":      {"mode": "daily",   "start": "2010-01-01"},
    # 週K / 月K (date 慣例：當週週一 / 當月 1 號)
    "TaiwanStockWeekPrice":               {"mode": "weekly",  "start": "2000-01-03"},
    "TaiwanStockMonthPrice":              {"mode": "monthly", "start": "2000-01-01"},
}

# 預設要跑的全部資料集 (依登錄表順序)
ALL_DATASETS = list(REGISTRY.keys())

REQUEST_DELAY = 0.5          # 每次成功請求後的禮貌間隔 (秒)
MIN_REQUEST_INTERVAL = 1.0   # 每次有發出網路請求後的最小間隔 (含 nodata/fail)
TIMEOUT = (10, 600)          # (連線, 讀取) 逾時；每5秒指數單日可達數十萬列
MAX_RETRIES = 5
BACKOFF_BASE = 5
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
    """'08:00-14:30' → (start_minutes, end_minutes)；None 表示不啟用。"""
    if not s:
        return None
    a, b = s.split("-")
    ah, am = map(int, a.split(":"))
    bh, bm = map(int, b.split(":"))
    return (ah * 60 + am, bh * 60 + bm)


def wait_if_blackout(blackout):
    """若目前在黑名單時段內，迴圈等待至時段結束 (每 60s 檢查)。"""
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


def weekly_dates(start: date, end: date):
    """回傳區間內所有週一 (週K 的 date 慣例)。"""
    d = start + timedelta(days=(7 - start.weekday()) % 7) if start.weekday() != 0 else start
    while d <= end:
        yield d
        d += timedelta(days=7)


def monthly_dates(start: date, end: date):
    """回傳區間內所有每月 1 號 (月K 的 date 慣例)。"""
    d = date(start.year, start.month, 1)
    while d <= end:
        yield d
        d = date(d.year + (d.month == 12), (d.month % 12) + 1, 1)


def daily_dates(start: date, end: date):
    """回傳區間內所有工作日 (跳過週六日；台股無週六交易)。"""
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def out_path_dated(dataset: str, d: str) -> Path:
    return OUT_ROOT / dataset / d[:4] / f"{dataset}_{d}.parquet"


def out_path_snapshot(dataset: str) -> Path:
    return OUT_ROOT / dataset / f"{dataset}.parquet"


def is_quota_msg(msg: str) -> bool:
    m = msg.lower()
    return "limit" in m or "402" in m or ("request" in m and "exceed" in m)


def api_get(session, token, params):
    """回傳 (rows, status_str)。status_str: 'ok'/'nodata'/'fail'。
    402/額度 → 暫停等配額重置後重試 (不丟資料)；401/403 → 立即 sys.exit。"""
    headers = {"Authorization": f"Bearer {token}"}
    quota_pauses = 0
    attempt = 0
    while True:
        attempt += 1
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
            log("    -> token 可能失效或無此資料權限 (部分資料集限 backer/sponsor)。中止。")
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


def download_snapshot(session, token, dataset, params_extra=None):
    """整表快照 / range_snapshot：抓回整表覆寫單一檔。"""
    params = {"dataset": dataset}
    if params_extra:
        params.update(params_extra)
    rows, status = api_get(session, token, params)
    if status == "ok":
        dest = out_path_snapshot(dataset)
        df = save_parquet(rows, dest)
        log(f"  [{dataset}] 快照 OK  rows={len(df):,} size={dest.stat().st_size/1e6:.1f}MB -> {dest.name}")
    elif status == "nodata":
        log(f"  [{dataset}] 快照無資料")
    else:
        log(f"  [{dataset}] 快照失敗")
    return status


def download_dated(session, token, dataset, d: str) -> str:
    """逐日/週/月 整個市場：start=end=該日，每日一檔。回 ok/skip/nodata/fail。"""
    dest = out_path_dated(dataset, d)
    if dest.exists() and dest.stat().st_size > 0:
        return "skip"
    rows, status = api_get(session, token, {"dataset": dataset, "start_date": d, "end_date": d})
    if status == "ok":
        df = save_parquet(rows, dest)
        log(f"  [{dataset}] {d} OK  rows={len(df):,} size={dest.stat().st_size/1e6:.1f}MB")
    return status


def main():
    ap = argparse.ArgumentParser(description="下載 FinMind 技術面 (/data) 資料集")
    ap.add_argument("--datasets", default=",".join(ALL_DATASETS),
                    help="逗號分隔；預設全部技術面 (/data) 資料集")
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

    log(f"==== 開始下載技術面資料集 {datasets} | end={args.end} | "
        f"{'reverse' if args.reverse else 'forward'} | blackout={args.blackout or '無'} ====")

    for dataset in datasets:
        spec = REGISTRY[dataset]
        mode = spec["mode"]
        wait_if_blackout(blackout)

        if mode == "snapshot":
            download_snapshot(session, token, dataset)
            time.sleep(MIN_REQUEST_INTERVAL)
            continue

        if mode == "range_snapshot":
            start = spec["start"]   # 整表覆寫一律抓全史，忽略 --start(避免每日 40 天視窗截斷歷史)
            download_snapshot(session, token, dataset,
                              {"start_date": start, "end_date": args.end})
            time.sleep(MIN_REQUEST_INTERVAL)
            continue

        # per-date 模式 (daily / weekly / monthly)
        start = datetime.strptime(args.start or spec["start"], "%Y-%m-%d").date()
        if mode == "daily":
            days = list(daily_dates(start, end))
        elif mode == "weekly":
            days = list(weekly_dates(start, end))
        elif mode == "monthly":
            days = list(monthly_dates(start, end))
        else:
            log(f"  [{dataset}] 未知 mode={mode}，跳過")
            continue
        if args.reverse:
            days.reverse()

        counts = {"ok": 0, "skip": 0, "nodata": 0, "fail": 0}
        failed = []
        log(f"---- {dataset} ({mode}) 共 {len(days)} 個候選日 ----")
        for i, dd in enumerate(days, 1):
            wait_if_blackout(blackout)
            d = dd.isoformat()
            status = download_dated(session, token, dataset, d)
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

    log("==== 技術面下載流程結束 ====")


if __name__ == "__main__":
    main()
