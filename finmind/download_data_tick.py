#!/usr/bin/env python3
"""
下載 FinMind /api/v4/data 端點的逐筆資料：
  - TaiwanFuturesTick (期貨交易明細表)
  - TaiwanOptionTick  (選擇權交易明細表)

模式：逐日、逐商品 (data_id) 下載後合併成一天一檔，回傳 JSON 轉存為 parquet。
  2026-09-19 起這兩個資料集必須帶 data_id (不帶回 HTTP 400)，「一次拿整日所有商品」的寫法失效。
  要抓哪些商品由日成交資料集決定 (TaiwanFuturesDaily / TaiwanOptionDaily 可不帶 data_id)：
  取當日成交量 > 0 的商品 (以 2026-09-18 的整日檔驗證，與逐筆檔內的商品完全一致)。

一檔是一個「日曆日」(00:00~24:00)。其中 15:00~24:00 的夜盤屬於下一個交易日，
FinMind 要到下一個交易日的資料出來才有，所以：
  - 當天抓的是暫定檔 (旁邊有 <檔名>.partial 標記)，只到日盤結束；
  - 下一個交易日的日成交出來後，補抓當晚有夜盤成交的商品併入，移除標記 (定稿)；
  - 週六沒有日盤、只有 00:00~05:00 的夜盤尾段，等下一個交易日出來後一次抓定稿。
沒有標記的既有檔視為定稿，自動跳過 (可中斷續傳)。有標記的檔即使不在 --start/--end 內也會補。

輸出：/mnt/d/finmind_data/<dataset>/<年>/<dataset>_<日期>.parquet
（此資料集「不」做 Google Drive 同步）

用法：
  # 預設：兩個資料集，從近期往回下載到 2019-01-01
  python download_data_tick.py

  # 指定資料集 / 日期區間 / 順序
  python download_data_tick.py --datasets TaiwanFuturesTick --start 2019-01-01 --end 2026-06-01 --reverse

注意：期貨/選擇權偶有週六補班交易日，故「不」跳過週六；僅週日固定無交易而跳過以省請求。
有任何一天失敗時以非 0 結束 (排程據此告警)；一天約 350~400 次請求。
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
# 逐筆資料集 -> (日成交資料集, 商品代號欄)
DAILY_OF = {
    "TaiwanFuturesTick": ("TaiwanFuturesDaily", "futures_id"),
    "TaiwanOptionTick": ("TaiwanOptionDaily", "option_id"),
}
DEFAULT_START = "2019-01-01"

MIN_REQUEST_INTERVAL = 1.0   # 每次發出網路請求的最小間隔 (秒)，避免短時間爆量請求
TIMEOUT = (10, 600)          # (連線, 讀取) 逾時；台指選擇權單日可達 60MB+
MAX_RETRIES = 5
BACKOFF_BASE = 5
# 連續這麼多「天」失敗就熔斷 (跨資料集累計)，避免系統性錯誤持續打 API
MAX_CONSECUTIVE_FAILURES = 8
# 額度達上限時暫停等配額重置
QUOTA_PAUSE = 600
QUOTA_MAX_PAUSES = 24
# 找下一個交易日最多往後看幾天 (涵蓋春節連假)
MAX_LOOKAHEAD = 12


class ApiFail(Exception):
    """單一請求重試後仍失敗。"""


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


def out_path(dataset: str, d: str, out_root: Path = OUT_ROOT) -> Path:
    return out_root / dataset / d[:4] / f"{dataset}_{d}.parquet"


def partial_marker(dest: Path) -> Path:
    return dest.with_name(dest.name + ".partial")


def pending_days(dataset: str, out_root: Path = OUT_ROOT) -> list[date]:
    """還掛著 .partial 標記 (夜盤待補) 的日期。"""
    days = []
    for p in (out_root / dataset).glob(f"*/{dataset}_*.parquet.partial"):
        try:
            days.append(datetime.strptime(p.name[len(dataset) + 1:][:10], "%Y-%m-%d").date())
        except ValueError:
            continue
    return days


class Fetcher:
    """帶節流／重試／額度等待的 GET。失敗丟 ApiFail；token 失效直接中止。"""

    def __init__(self, token: str, interval: float = MIN_REQUEST_INTERVAL):
        self.session = requests.Session()
        self.headers = {"Authorization": f"Bearer {token}"}
        self.interval = interval
        self.requests = 0
        self._last = 0.0

    def __call__(self, params: dict, label: str) -> list[dict]:
        quota_pauses = 0
        attempt = 0
        while True:
            attempt += 1
            wait = self._last + self.interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.requests += 1
            try:
                resp = self.session.get(URL, headers=self.headers, params=params, timeout=TIMEOUT)
            except requests.RequestException as e:
                if attempt > MAX_RETRIES:
                    raise ApiFail(f"{label} 連線重試 {MAX_RETRIES} 次仍失敗 ({e})") from e
                wait = BACKOFF_BASE * attempt
                log(f"  [{label}] 連線錯誤 ({e})，{wait}s 後重試 ({attempt}/{MAX_RETRIES})")
                time.sleep(wait)
                continue

            code = resp.status_code

            if code == 200:
                try:
                    payload = resp.json()
                except ValueError:
                    raise ApiFail(f"{label} 回傳非 JSON") from None
                if payload.get("status") == 200:
                    return payload.get("data", [])
                msg = str(payload.get("msg", ""))
                if "limit" not in msg.lower() and "402" not in msg:
                    raise ApiFail(f"{label} API 回應非 200: {msg}")
                quota_msg = msg                       # 額度類訊息 → 與 402 同樣暫停
            elif code == 402:
                quota_msg = "HTTP 402 達 API 上限"
            elif code in (401, 403):
                log(f"  [{label}] 權限錯誤 HTTP {code}: {resp.text[:200]}")
                log("  -> token 可能失效或無此資料權限。中止。")
                sys.exit(2)
            elif code == 429 or code >= 500:
                if attempt > MAX_RETRIES:
                    raise ApiFail(f"{label} HTTP {code} 重試 {MAX_RETRIES} 次仍失敗")
                wait = BACKOFF_BASE * attempt
                log(f"  [{label}] HTTP {code}，{wait}s 後重試 ({attempt}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            else:
                raise ApiFail(f"{label} 非預期 HTTP {code}: {resp.text[:200]}")

            # 額度達上限：暫停等配額重置 (不丟資料)
            quota_pauses += 1
            if quota_pauses > QUOTA_MAX_PAUSES:
                raise ApiFail(f"{label} 額度已暫停 {QUOTA_MAX_PAUSES} 次仍未恢復")
            log(f"  [{label}] [額度] {quota_msg}，暫停 {QUOTA_PAUSE}s 等配額重置 "
                f"({quota_pauses}/{QUOTA_MAX_PAUSES})")
            time.sleep(QUOTA_PAUSE)
            attempt = 0


class Collector:
    """fetch(params, label) -> list[dict]；抽出來讓測試可以換成假的。"""

    def __init__(self, fetch, out_root: Path = OUT_ROOT, today: date | None = None):
        self.fetch = fetch
        self.out_root = Path(out_root)
        self.today = today or date.today()
        self._daily: dict[tuple[str, date], pd.DataFrame] = {}

    def daily(self, dataset: str, d: date) -> pd.DataFrame:
        """該交易日的日成交 (全部商品)；非交易日／尚未公布為空表。同一次執行內快取。"""
        key = (dataset, d)
        if key not in self._daily:
            daily_ds, _ = DAILY_OF[dataset]
            ds = d.isoformat()
            rows = self.fetch({"dataset": daily_ds, "start_date": ds, "end_date": ds},
                              f"{daily_ds} {ds}")
            self._daily[key] = pd.DataFrame(rows)
        return self._daily[key]

    def traded_ids(self, dataset: str, d: date, session: str | None = None) -> set[str]:
        """交易日 d 有成交的商品；session='after_market' 只算夜盤 (前一日 15:00 ~ 當日 05:00)。"""
        df = self.daily(dataset, d)
        if df.empty:
            return set()
        hit = pd.to_numeric(df["volume"], errors="coerce").fillna(0) > 0
        if session:
            hit &= df["trading_session"] == session
        return set(df.loc[hit, DAILY_OF[dataset][1]].astype(str))

    def next_trading_day(self, dataset: str, d: date) -> date | None:
        """d 之後第一個已公布日成交的交易日；還沒有就回 None。"""
        for i in range(1, MAX_LOOKAHEAD + 1):
            nd = d + timedelta(days=i)
            if nd > self.today:
                return None
            if not self.daily(dataset, nd).empty:
                return nd
        return None

    def download_day(self, dataset: str, d: date) -> str:
        """回傳 'ok' (定稿) / 'partial' (暫定，夜盤待補) / 'skip' / 'nodata' / 'fail'"""
        ds = d.isoformat()
        id_col = DAILY_OF[dataset][1]
        dest = out_path(dataset, ds, self.out_root)
        marker = partial_marker(dest)
        exists = dest.exists() and dest.stat().st_size > 0
        if exists and not marker.exists():
            return "skip"

        try:
            nxt = self.next_trading_day(dataset, d)
            if exists and nxt is None:
                return "skip"                 # 暫定檔，下一個交易日還沒出來
            evening = self.traded_ids(dataset, nxt, "after_market") if nxt else set()
            if exists:
                # 定稿：只重抓當晚有夜盤成交的商品 (其餘商品暫定檔裡已是全日)
                old = pd.read_parquet(dest)
                had = set(old[id_col].astype(str))
                ids = evening | (self.traded_ids(dataset, d) - had)
                frames = [old[~old[id_col].isin(ids)]]
            else:
                had = set()
                ids = self.traded_ids(dataset, d) | evening
                frames = []
            for data_id in sorted(ids):
                rows = self.fetch({"dataset": dataset, "data_id": data_id, "start_date": ds},
                                  f"{dataset} {ds} {data_id}")
                if rows:
                    frames.append(pd.DataFrame(rows))
                elif data_id in had:
                    # 暫定檔裡有、重抓卻是空的：寧可這次不定稿，也不要把已有的資料換掉
                    raise ApiFail(f"{dataset} {ds} {data_id} 重抓回傳空資料")
        except ApiFail as e:
            log(f"  [{dataset}] {ds} 失敗: {e}")
            return "fail"

        frames = [f for f in frames if not f.empty]
        if not frames:
            return "nodata"

        df = pd.concat(frames, ignore_index=True)
        df = df.sort_values([id_col, "date"], kind="stable", ignore_index=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".parquet.tmp")
        df.to_parquet(tmp, index=False)
        final = nxt is not None
        if not final:
            marker.touch()                    # 先掛標記再換檔，中斷也不會留下沒標記的暫定檔
        tmp.replace(dest)
        if final:
            marker.unlink(missing_ok=True)
        note = "定稿" if final else "暫定，夜盤待下一交易日補"
        if exists:
            note += f"；補夜盤 {len(ids)} 檔商品"
        log(f"  [{dataset}] {ds} OK  rows={len(df):,} 商品={df[id_col].nunique()} "
            f"size={dest.stat().st_size/1e6:.1f}MB ({note})")
        return "ok" if final else "partial"


def run(collector: Collector, datasets: list[str], start: date, end: date, reverse: bool) -> int:
    """回傳 exit code：0 全部成功、1 有失敗的日子、3 連續失敗熔斷。"""
    rc = 0
    consecutive_failures = 0
    for dataset in datasets:
        days = sorted(set(daterange(start, end, False)) | set(pending_days(dataset, collector.out_root)),
                      reverse=reverse)
        counts = {"ok": 0, "partial": 0, "skip": 0, "nodata": 0, "fail": 0}
        failed = []
        log(f"---- 資料集 {dataset} ({len(days)} 天) ----")
        for i, dd in enumerate(days, 1):
            status = collector.download_day(dataset, dd)
            counts[status] += 1

            if status == "fail":
                failed.append(dd.isoformat())
                rc = 1
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log(f"==== 連續 {consecutive_failures} 天失敗，熔斷中止 (避免 IP 被封鎖) ====")
                    return 3
            else:
                consecutive_failures = 0

            if i % 50 == 0:
                log(f"  [{dataset}] 進度 {i}/{len(days)}  新={counts['ok']} 暫定={counts['partial']} "
                    f"跳過={counts['skip']} 無資料={counts['nodata']} 失敗={counts['fail']}")
        log(f"==== {dataset} 完成 ====  新={counts['ok']} 暫定={counts['partial']} 已存在={counts['skip']} "
            f"無資料={counts['nodata']} 失敗={counts['fail']}")
        if failed:
            log(f"  [{dataset}] 失敗日期: {', '.join(failed)}")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="下載 FinMind 期貨/選擇權逐筆資料 (逐商品下載，合併成一天一檔)")
    ap.add_argument("--datasets", default=",".join(ALL_DATASETS),
                    help=f"逗號分隔，預設 {','.join(ALL_DATASETS)}")
    ap.add_argument("--start", default=DEFAULT_START, help="起始日期 (預設 2019-01-01)")
    ap.add_argument("--end", default=date.today().isoformat(), help="結束日期 (預設今天)")
    ap.add_argument("--reverse", action="store_true", help="從最近日期往回下載")
    ap.add_argument("--interval", type=float, default=MIN_REQUEST_INTERVAL,
                    help=f"每次請求的最小間隔秒數 (預設 {MIN_REQUEST_INTERVAL}；大量回補時調大，留額度給其他排程)")
    ap.add_argument("--out-root", type=Path, default=OUT_ROOT, help=f"輸出根目錄 (預設 {OUT_ROOT})")
    args = ap.parse_args()

    datasets = [s.strip() for s in args.datasets.split(",") if s.strip()]
    s = datetime.strptime(args.start, "%Y-%m-%d").date()
    e = datetime.strptime(args.end, "%Y-%m-%d").date()

    fetch = Fetcher(load_token(), args.interval)
    log(f"==== 開始下載 {datasets} | {args.start}~{args.end} | "
        f"{'reverse' if args.reverse else 'forward'} ====")
    rc = run(Collector(fetch, args.out_root), datasets, s, e, args.reverse)
    log(f"==== 結束 rc={rc}，共 {fetch.requests} 次請求 ====")
    return rc


if __name__ == "__main__":
    sys.exit(main())
