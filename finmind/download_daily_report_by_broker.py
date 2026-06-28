#!/usr/bin/env python3
"""
逐「券商分點代碼」下載分點報表 / 權證分點報表 (SPONSOR 降級後用)。

背景：
  SPONSORPRO 可用 storage_objects「一次拿特定日期、全市場」bulk 下載 (1 請求/日)；
  降級為 SPONSOR 後該 bulk 不可用，分點資料須改走「專屬 endpoint + securities_trader_id」
  逐券商代碼、單一日期查詢，再把所有券商的回傳併成「該日完整報表」。

  本腳本即此 fallback。輸出檔路徑/欄位與 bulk 版完全相同 (drop-in 取代)，故未來只要把
  daily 排程從 download_tick.py(storage_objects) 換成本腳本即可。

兩資料集 (專屬 endpoint，單一 date，by securities_trader_id；data_id 形式無效)：
  TaiwanStockTradingDailyReport        → /api/v4/taiwan_stock_trading_daily_report
  TaiwanStockWarrantTradingDailyReport → /api/v4/taiwan_stock_warrant_trading_daily_report
  回傳欄位皆：securities_trader, price, buy, sell, securities_trader_id, stock_id, date

券商代碼來源：securities_trader_ids.parquet (build_trader_ids.py 產生：master ∪ 歷史觀察)，
  執行時再 union 當前 master TaiwanSecuritiesTraderInfo (抓新開分點)。

防封鎖：402/額度自動暫停等配額重置、連續失敗熔斷、每請求最小間隔、401/403 立即中止、原子寫檔。
盤中讓道：--blackout 08:00-14:30。skip-existing：該日 parquet 已存在則跳過 (除非 --refresh)。

用法：
  python download_daily_report_by_broker.py --dataset TaiwanStockWarrantTradingDailyReport --date 2026-06-26
  python download_daily_report_by_broker.py --dataset TaiwanStockTradingDailyReport --start 2026-06-20 --end 2026-06-27 --reverse
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
OUT_ROOT = Path("/mnt/d/finmind_data")
LOG_FILE = BASE_DIR / "download_daily_report_by_broker.log"
TRADER_IDS_FILE = BASE_DIR / "securities_trader_ids.parquet"
MASTER = OUT_ROOT / "TaiwanSecuritiesTraderInfo" / "TaiwanSecuritiesTraderInfo.parquet"

ENDPOINTS = {
    "TaiwanStockTradingDailyReport":
        "https://api.finmindtrade.com/api/v4/taiwan_stock_trading_daily_report",
    "TaiwanStockWarrantTradingDailyReport":
        "https://api.finmindtrade.com/api/v4/taiwan_stock_warrant_trading_daily_report",
}
COLUMNS = ["securities_trader", "price", "buy", "sell", "securities_trader_id", "stock_id", "date"]

REQUEST_DELAY = 0.25
MIN_REQUEST_INTERVAL = 0.5
TIMEOUT = (10, 300)
MAX_RETRIES = 5
BACKOFF_BASE = 5
MAX_CONSECUTIVE_FAILURES = 12
QUOTA_PAUSE = 600
QUOTA_MAX_PAUSES = 24


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_token() -> str:
    token = dotenv_values(BASE_DIR / ".env").get("finmind_api_key", "").strip()
    if not token:
        log("ERROR: .env 找不到 finmind_api_key")
        sys.exit(1)
    return token


def parse_blackout(s):
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
                log(f"  [黑名單] 盤中暫停，等待至 {blackout[1]//60:02d}:{blackout[1]%60:02d}")
                announced = True
            time.sleep(60)
        else:
            if announced:
                log("  [黑名單] 時段結束，恢復下載")
            return


def is_quota_msg(msg: str) -> bool:
    m = msg.lower()
    return "limit" in m or "402" in m or ("request" in m and "exceed" in m)


def api_get(session, token, url, params, blackout=None):
    """回傳 (rows, status)。402/額度→暫停等配額；401/403→sys.exit。"""
    headers = {"Authorization": f"Bearer {token}"}
    quota_pauses = 0
    attempt = 0
    while True:
        attempt += 1
        wait_if_blackout(blackout)
        try:
            resp = session.get(url, headers=headers, params=params, timeout=TIMEOUT)
        except requests.RequestException as e:
            if attempt > MAX_RETRIES:
                return None, "fail"
            time.sleep(BACKOFF_BASE * attempt)
            continue
        code = resp.status_code
        if code == 402:
            quota_pauses += 1
            if quota_pauses > QUOTA_MAX_PAUSES:
                return None, "fail"
            log(f"    [額度] HTTP 402，暫停 {QUOTA_PAUSE}s 等配額重置 ({quota_pauses}/{QUOTA_MAX_PAUSES})")
            time.sleep(QUOTA_PAUSE)
            attempt = 0
            continue
        if code == 200:
            try:
                payload = resp.json()
            except ValueError:
                return None, "fail"
            if payload.get("status") != 200:
                msg = str(payload.get("msg", ""))
                if is_quota_msg(msg):
                    quota_pauses += 1
                    if quota_pauses > QUOTA_MAX_PAUSES:
                        return None, "fail"
                    log(f"    [額度] {msg}，暫停 {QUOTA_PAUSE}s ({quota_pauses}/{QUOTA_MAX_PAUSES})")
                    time.sleep(QUOTA_PAUSE)
                    attempt = 0
                    continue
                return None, "fail"
            rows = payload.get("data", []) or []
            return rows, ("ok" if rows else "nodata")
        if code in (401, 403):
            log(f"    權限錯誤 HTTP {code} → token 失效或無權限 (降級後仍須 SPONSOR)。中止。")
            sys.exit(2)
        if code == 429 or code >= 500:
            if attempt > MAX_RETRIES:
                return None, "fail"
            time.sleep(BACKOFF_BASE * attempt)
            continue
        return None, "fail"


def load_broker_codes() -> list:
    codes = set()
    if TRADER_IDS_FILE.exists():
        codes |= set(pd.read_parquet(TRADER_IDS_FILE)["securities_trader_id"].astype(str))
        log(f"  券商代碼讀自 {TRADER_IDS_FILE.name}：{len(codes)} 個")
    else:
        log(f"  WARNING: 找不到 {TRADER_IDS_FILE.name}，請先跑 build_trader_ids.py")
    # 執行時 union 當前 master，補新開分點
    if MASTER.exists():
        m = set(pd.read_parquet(MASTER, columns=["securities_trader_id"])["securities_trader_id"].astype(str))
        new = m - codes
        if new:
            log(f"  自當前 master 補入 {len(new)} 個新代碼")
        codes |= m
    return sorted(codes)


def out_path(dataset, d: str) -> Path:
    return OUT_ROOT / dataset / d[:4] / f"{dataset}_{d}.parquet"


def save_atomic(df: pd.DataFrame, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(dest)


def download_one_date(session, token, url, dataset, d: str, codes, blackout, refresh, fail_state):
    dest = out_path(dataset, d)
    if not refresh and dest.exists() and dest.stat().st_size > 0:
        log(f"  [{dataset}] {d} 已存在，跳過")
        return "skip"
    frames = []
    nbrokers = ok = nod = fail = 0
    for sid in codes:
        rows, status = api_get(session, token, url, {"securities_trader_id": sid, "date": d}, blackout)
        if status == "ok":
            frames.append(pd.DataFrame(rows))
            ok += 1
        elif status == "nodata":
            nod += 1
        else:
            fail += 1
            fail_state[0] += 1
            if fail_state[0] >= MAX_CONSECUTIVE_FAILURES:
                log(f"==== 連續 {fail_state[0]} 次失敗，熔斷中止 ====")
                sys.exit(3)
        if status != "fail":
            fail_state[0] = 0
        nbrokers += 1
        time.sleep(REQUEST_DELAY if status == "ok" else MIN_REQUEST_INTERVAL)
        if nbrokers % 200 == 0:
            log(f"    [{dataset}] {d} 進度 {nbrokers}/{len(codes)}  有料券商={ok} 無料={nod} 失敗={fail}")
    if not frames:
        log(f"  [{dataset}] {d} 全部券商皆無資料 (可能非交易日)")
        return "nodata"
    df = pd.concat(frames, ignore_index=True)
    # 對齊 bulk 欄位順序
    df = df[[c for c in COLUMNS if c in df.columns]]
    save_atomic(df, dest)
    log(f"  [{dataset}] {d} OK  rows={len(df):,} 有料券商={ok}/{len(codes)} "
        f"stocks={df['stock_id'].nunique():,} size={dest.stat().st_size/1e6:.1f}MB")
    return "ok"


def weekday_dates(start: date, end: date, reverse):
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return list(reversed(days)) if reverse else days


def main():
    ap = argparse.ArgumentParser(description="逐券商代碼下載分點/權證分點報表 (SPONSOR fallback)")
    ap.add_argument("--dataset", required=True, choices=list(ENDPOINTS),
                    help="TaiwanStockTradingDailyReport 或 TaiwanStockWarrantTradingDailyReport")
    ap.add_argument("--date", help="單一日期 YYYY-MM-DD")
    ap.add_argument("--start", help="起始日期 (與 --end 搭配)")
    ap.add_argument("--end", help="結束日期")
    ap.add_argument("--reverse", action="store_true", help="從最近往回")
    ap.add_argument("--refresh", action="store_true", help="覆寫既有日檔")
    ap.add_argument("--blackout", default="", help="盤中暫停時段，如 08:00-14:30")
    args = ap.parse_args()

    if not args.date and not (args.start and args.end):
        log("ERROR: 需 --date 或 --start+--end")
        sys.exit(1)

    blackout = parse_blackout(args.blackout)
    token = load_token()
    session = requests.Session()
    url = ENDPOINTS[args.dataset]
    codes = load_broker_codes()
    if not codes:
        sys.exit(1)

    if args.date:
        days = [datetime.strptime(args.date, "%Y-%m-%d").date()]
    else:
        s = datetime.strptime(args.start, "%Y-%m-%d").date()
        e = datetime.strptime(args.end, "%Y-%m-%d").date()
        days = weekday_dates(s, e, args.reverse)

    fail_state = [0]
    log(f"==== by-broker 下載 {args.dataset} | {len(days)} 日 × {len(codes)} 券商 | "
        f"blackout={args.blackout or '無'} {'[refresh]' if args.refresh else ''} ====")
    counts = {"ok": 0, "skip": 0, "nodata": 0}
    for dd in days:
        wait_if_blackout(blackout)
        st = download_one_date(session, token, url, args.dataset, dd.isoformat(),
                               codes, blackout, args.refresh, fail_state)
        counts[st] = counts.get(st, 0) + 1
    log(f"==== 完成 {args.dataset} ==== 新={counts['ok']} 跳過={counts['skip']} 無資料={counts['nodata']}")


if __name__ == "__main__":
    main()
