#!/usr/bin/env python3
"""
下載 FinMind「台股 - 基本面」資料集 (走 /api/v4/data 端點)。

取得模式 (見 REGISTRY，皆由 API 實測決定)：
  - quarterly      : 逐「季底日」(03-31/06-30/09-30/12-31)、整個市場 (不帶 data_id)，每季一檔
                     綜合損益表 / 資產負債表 / 現金流量表 (date=季底；非季底回 nodata)
  - monthly        : 逐「每月 1 號」、整個市場，每月一檔
                     月營收表 (date=YYYY-MM-01，內含 revenue_year/revenue_month)
  - daily          : 逐交易日、整個市場，每日一檔
                     股價市值表 (date=交易日)
  - snapshot       : 整表快照 (不帶任何日期)，覆寫單一檔
                     下市櫃表 (全歷史一次回，僅 341 列)
  - range_snapshot : 一次帶整段 start~today 取回整表，覆寫單一檔 (稀疏公告類)
                     減資參考價 / 分割後參考價 / 變更面額參考價
  - per_stock      : 逐股票 (必帶 data_id；此三集「不帶 data_id 依日期查」只回殘缺資料)，
                     每檔股票存一個含全歷史的檔
                     股利政策表 / 除權除息結果表 / 市值比重表
                     股票清單取自 /mnt/d/finmind_data/TaiwanStockInfo/TaiwanStockInfo.parquet
                     (缺檔時改打 TaiwanStockInfo API)

API 額度保護 (常與其他 backfill 同時跑)：
  HTTP 402 / 額度訊息 → 暫停 QUOTA_PAUSE 秒後重試 (配額每小時重置)，最多 QUOTA_MAX_PAUSES 次；
  401/403 (token 失效/無權限) 立即中止。另含連續失敗熔斷、每請求最小間隔、原子寫檔可安全中斷續傳。

盤中讓道：--blackout 08:00-14:30 期間自動暫停 (不發請求)，時段外自動續跑。

用法：
  python download_fundamental.py --reverse --blackout 08:00-14:30        # 全部，從近期往回回補
  python download_fundamental.py --datasets TaiwanStockMonthRevenue --reverse
  python download_fundamental.py --refresh                               # 重抓快照/逐股票類 (更新近期)
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
LOG_FILE = BASE_DIR / "download_fundamental.log"
STOCK_INFO_PARQUET = OUT_ROOT / "TaiwanStockInfo" / "TaiwanStockInfo.parquet"

# 資料集登錄表：mode + 已知最早可用日期 (API 探測得出)
REGISTRY = {
    # 逐季底日、整個市場
    "TaiwanStockFinancialStatements": {"mode": "quarterly", "start": "1999-01-01"},
    "TaiwanStockBalanceSheet":        {"mode": "quarterly", "start": "1999-01-01"},
    "TaiwanStockCashFlowsStatement":  {"mode": "quarterly", "start": "1999-01-01"},
    # 逐每月 1 號、整個市場
    "TaiwanStockMonthRevenue":        {"mode": "monthly",   "start": "2001-01-01"},
    # 逐交易日、整個市場
    "TaiwanStockMarketValue":         {"mode": "daily",     "start": "2005-01-01"},
    # 整表快照 (不帶日期)
    "TaiwanStockDelisting":           {"mode": "snapshot"},
    # 稀疏公告：整段一次抓，覆寫單檔
    "TaiwanStockCapitalReductionReferencePrice": {"mode": "range_snapshot", "start": "2000-01-01"},
    "TaiwanStockSplitPrice":          {"mode": "range_snapshot", "start": "2000-01-01"},
    "TaiwanStockParValueChange":      {"mode": "range_snapshot", "start": "2000-01-01"},
    # 逐股票 (必帶 data_id)
    "TaiwanStockDividend":            {"mode": "per_stock", "start": "2000-01-01"},
    "TaiwanStockDividendResult":      {"mode": "per_stock", "start": "2000-01-01"},
    "TaiwanStockMarketValueWeight":   {"mode": "per_stock", "start": "2010-01-01"},
}
ALL_DATASETS = list(REGISTRY.keys())

REQUEST_DELAY = 0.5
MIN_REQUEST_INTERVAL = 1.0
TIMEOUT = (10, 600)
MAX_RETRIES = 5
BACKOFF_BASE = 5
MAX_CONSECUTIVE_FAILURES = 8
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
    return "limit" in m or "402" in m or ("request" in m and "exceed" in m)


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


def out_path_stock(dataset: str, sid: str) -> Path:
    return OUT_ROOT / dataset / "by_stock" / f"{dataset}_{sid}.parquet"


# ---- 日期序列產生 ----
def quarter_end_dates(start: date, end: date):
    for y in range(start.year, end.year + 1):
        for mm, dd in ((3, 31), (6, 30), (9, 30), (12, 31)):
            qd = date(y, mm, dd)
            if start <= qd <= end:
                yield qd


def month_first_dates(start: date, end: date):
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        md = date(y, m, 1)
        if md >= start:
            yield md
        m += 1
        if m > 12:
            m, y = 1, y + 1


def trading_dates(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def load_universe() -> list:
    """逐股票模式所需的股票清單；優先讀已存在的 TaiwanStockInfo.parquet，否則打 API。"""
    if STOCK_INFO_PARQUET.exists():
        try:
            df = pd.read_parquet(STOCK_INFO_PARQUET, columns=["stock_id"])
            ids = sorted(df["stock_id"].dropna().astype(str).unique())
            if ids:
                log(f"  股票清單取自 {STOCK_INFO_PARQUET.name}：{len(ids)} 檔")
                return ids
        except Exception as e:
            log(f"  讀取 {STOCK_INFO_PARQUET} 失敗 ({e})，改打 API")
    token = load_token()
    rows, status = api_get(requests.Session(), token, {"dataset": "TaiwanStockInfo"})
    ids = sorted({str(r["stock_id"]) for r in (rows or []) if r.get("stock_id")})
    log(f"  股票清單取自 API：{len(ids)} 檔")
    return ids


# ---- 各模式下載 ----
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


def download_stock(session, token, dataset, sid, start, end, blackout=None, refresh=False) -> str:
    dest = out_path_stock(dataset, sid)
    if not refresh and dest.exists() and dest.stat().st_size > 0:
        return "skip"
    rows, status = api_get(session, token,
                           {"dataset": dataset, "data_id": sid, "start_date": start, "end_date": end},
                           blackout)
    if status == "ok":
        save_parquet(rows, dest)
    return status


def run_dated(session, token, dataset, mode, days, reverse, blackout, fail_state):
    if reverse:
        days = list(reversed(days))
    counts = {"ok": 0, "skip": 0, "nodata": 0, "fail": 0}
    failed = []
    log(f"---- {dataset} ({mode}) 共 {len(days)} 個候選日 ----")
    for i, dd in enumerate(days, 1):
        d = dd.isoformat()
        status = download_dated(session, token, dataset, d, blackout)
        counts[status] += 1
        if status == "fail":
            failed.append(d)
            fail_state[0] += 1
            if fail_state[0] >= MAX_CONSECUTIVE_FAILURES:
                log(f"==== 連續 {fail_state[0]} 次失敗，熔斷中止 (避免 IP 被封鎖) ====")
                sys.exit(3)
        else:
            fail_state[0] = 0
        if status != "skip":
            time.sleep(REQUEST_DELAY if status == "ok" else MIN_REQUEST_INTERVAL)
        if i % 100 == 0:
            log(f"  [{dataset}] 進度 {i}/{len(days)}  新={counts['ok']} 跳過={counts['skip']} "
                f"無資料={counts['nodata']} 失敗={counts['fail']}")
    log(f"==== {dataset} 完成 ====  新={counts['ok']} 已存在={counts['skip']} "
        f"無資料={counts['nodata']} 失敗={counts['fail']}")
    if failed:
        log(f"  [{dataset}] 失敗日期: {', '.join(failed[:30])}{' ...' if len(failed)>30 else ''}")


def run_per_stock(session, token, dataset, start, end, universe, reverse, blackout, refresh, fail_state):
    ids = list(reversed(universe)) if reverse else list(universe)
    counts = {"ok": 0, "skip": 0, "nodata": 0, "fail": 0}
    failed = []
    log(f"---- {dataset} (per_stock) 共 {len(ids)} 檔股票 | start={start} {'[refresh]' if refresh else ''} ----")
    for i, sid in enumerate(ids, 1):
        status = download_stock(session, token, dataset, sid, start, end, blackout, refresh)
        counts[status] += 1
        if status == "fail":
            failed.append(sid)
            fail_state[0] += 1
            if fail_state[0] >= MAX_CONSECUTIVE_FAILURES:
                log(f"==== 連續 {fail_state[0]} 次失敗，熔斷中止 (避免 IP 被封鎖) ====")
                sys.exit(3)
        else:
            fail_state[0] = 0
        if status != "skip":
            time.sleep(REQUEST_DELAY if status == "ok" else MIN_REQUEST_INTERVAL)
        if i % 200 == 0:
            log(f"  [{dataset}] 進度 {i}/{len(ids)}  新={counts['ok']} 跳過={counts['skip']} "
                f"無資料={counts['nodata']} 失敗={counts['fail']}")
    log(f"==== {dataset} 完成 ====  新={counts['ok']} 已存在={counts['skip']} "
        f"無資料={counts['nodata']} 失敗={counts['fail']}")
    if failed:
        log(f"  [{dataset}] 失敗股票: {', '.join(failed[:30])}{' ...' if len(failed)>30 else ''}")


def main():
    ap = argparse.ArgumentParser(description="下載 FinMind 基本面 (/data) 資料集")
    ap.add_argument("--datasets", default=",".join(ALL_DATASETS),
                    help="逗號分隔；預設全部基本面 (/data) 資料集")
    ap.add_argument("--start", help="覆寫起始日期 (預設用各資料集已知最早日)")
    ap.add_argument("--end", default=date.today().isoformat(), help="結束日期 (預設今天)")
    ap.add_argument("--reverse", action="store_true", help="從最近往回下載")
    ap.add_argument("--blackout", default="", help="盤中暫停時段，如 08:00-14:30")
    ap.add_argument("--refresh", action="store_true",
                    help="重抓 snapshot/range_snapshot/per_stock 類 (覆寫既有檔，用於更新近期資料)")
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
    fail_state = [0]          # 跨資料集共用的連續失敗計數
    universe = None           # 延遲載入 (僅 per_stock 需要)

    log(f"==== 開始下載基本面資料集 {datasets} | end={args.end} | "
        f"{'reverse' if args.reverse else 'forward'} | blackout={args.blackout or '無'} | "
        f"{'refresh' if args.refresh else 'skip-existing'} ====")

    for dataset in datasets:
        spec = REGISTRY[dataset]
        mode = spec["mode"]
        wait_if_blackout(blackout)

        if mode == "snapshot":
            download_snapshot(session, token, dataset, blackout=blackout)
            time.sleep(MIN_REQUEST_INTERVAL)
            continue

        if mode == "range_snapshot":
            start = spec["start"]   # 整表覆寫一律抓全史，忽略 --start(避免每日視窗截斷歷史)
            download_snapshot(session, token, dataset,
                              {"start_date": start, "end_date": args.end}, blackout=blackout)
            time.sleep(MIN_REQUEST_INTERVAL)
            continue

        if mode in ("quarterly", "monthly", "daily"):
            start = datetime.strptime(args.start or spec["start"], "%Y-%m-%d").date()
            gen = {"quarterly": quarter_end_dates, "monthly": month_first_dates,
                   "daily": trading_dates}[mode]
            days = list(gen(start, end))
            run_dated(session, token, dataset, mode, days, args.reverse, blackout, fail_state)
            continue

        if mode == "per_stock":
            if universe is None:
                universe = load_universe()
            start = args.start or spec["start"]
            run_per_stock(session, token, dataset, start, args.end, universe,
                          args.reverse, blackout, args.refresh, fail_state)
            continue

    log("==== 基本面下載流程結束 ====")


if __name__ == "__main__":
    main()
