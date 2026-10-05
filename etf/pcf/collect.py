#!/usr/bin/env python3
"""ETF 申購買回清單 (PCF) 每日收集器: 每日申購贖回 + 每日成分 → D:\\etf_pcf (見 store.py)。

追蹤清單是 pcf/universe.csv (目前為 2026-10-02 市值前 50 大，七家投信)；來源與欄位對應見 sources.py。

每次執行，對每檔 ETF:
  1. 補近 BACK_DAYS 天內資料集還沒有的交易日 (較晚公告、前次失敗的都會在這裡補上)。
  2. 往後查到下一個交易日為止 —— 六家投信的「交易日」是清單公告日，收盤後當晚就公告下一個
     交易日的清單，所以晚上跑會寫入一列日期是「明天」的資料 (內容是今天收盤後的數字，
     「資料基準日」欄才是今天)。富邦以資料日為鍵，沒有這一步。
寫入以 (代號, 交易日) 為鍵，可重複執行；已有的日子不會再查。

「已有的日子不會再查」代表存進去的必須是定稿。以資料日為鍵的來源在盤中查當天，網站會先回
前一天的複本 (實測國泰持股明細: 2026-10-05 09 時查到的 10/05 與 10/02 股數、權重全同)，
所以當天的資料要 SAME_DAY_HOUR 點以後才查，而且內容與前一個已存日完全相同就不收，隔天再補。

用法:
  python pcf/collect.py                                   # 每日收集 (排程用)
  python pcf/collect.py --since 2026-10-01 --until 2026-10-05   # 回補指定區間 (已有的日子略過)
  python pcf/collect.py --codes 0050,00878 --dry-run      # 只抓不寫
  python pcf/collect.py --verify 2026-09-29,2026-09-30    # 重抓這幾天與資料集逐列比對 (驗證解析是否正確)
"""
from __future__ import annotations
import argparse
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store  # noqa: E402
from sources import SOURCES, Holdings, Summary  # noqa: E402

BACK_DAYS = 7          # 往回補缺的曆日數
FORWARD_DAYS = 6       # 往後找下一個交易日的曆日數 (涵蓋連假前一晚)
FORWARD_MISSES = 2     # 往後連續幾個平日查無資料就停 (清單還沒公告；隔天會當成缺漏補上)
SAME_DAY_HOUR = 17     # 以資料日為鍵的來源 (國泰/復華持股明細、富邦)，當天的資料幾點以後才查
SLEEP = 1.0            # 每查一個 (代號, 日期) 後的間隔秒數


def _d(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _weekdays(start: date, end: date) -> list[str]:
    out, cur = [], start
    while cur <= end:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def candidate_days(today: date, trading_days: set[str], since: str | None, until: str | None
                   ) -> tuple[list[str], list[str]]:
    """回傳 (要補的過去日子, 往後要試的日子)。過去的只取交易日；收盤價還沒進來的近日以平日代替。"""
    last_known = max(trading_days) if trading_days else "0000-00-00"
    if since:
        start, end = _d(since), min(_d(until), today) if until else today
    else:
        start, end = today - timedelta(days=BACK_DAYS), today
    past = [d for d in _weekdays(start, end) if d in trading_days or d > last_known]
    if since and until and _d(until) > today:
        future = _weekdays(today + timedelta(days=1), _d(until))
    elif since:
        future = []
    else:
        future = _weekdays(today + timedelta(days=1), today + timedelta(days=FORWARD_DAYS))
    return past, future


def summary_row(etf: pd.Series, s: Summary, cal: store.Calendar) -> dict:
    asof, basis = s.asof_day, store.BY_SOURCE
    if not asof:
        asof = store.summary_asof(etf["投信"], etf["市場"], s.trade_day, cal)
        basis = store.BY_RULE if asof else None
    return {"市值排名": etf["市值排名"], "代號": etf["代號"], "名稱": etf["名稱"], "投信": etf["投信"],
            "交易日": s.trade_day, "公告日期": s.announce_day, "資料基準日": asof, "基準日依據": basis,
            "狀態": store.HAS_DATA, "申購贖回淨增減單位數": s.net_units, "已發行單位數": s.units,
            "每單位淨值": s.nav, "基金淨資產": s.net_assets, "實物申贖基數": s.basket_units, "來源網址": s.url}


def constituent_frame(etf: pd.Series, h: Holdings, cal: store.Calendar) -> pd.DataFrame:
    asof, basis = h.asof_day, store.BY_SOURCE
    if not asof:
        asof = store.constituent_asof(etf["投信"], etf["市場"], h.source, h.trade_day, cal)
        basis = store.BY_RULE if asof else None
    df = pd.DataFrame(h.rows)
    df["ETF代號"], df["ETF名稱"], df["投信"] = etf["代號"], etf["名稱"], etf["投信"]
    df["交易日"], df["資料基準日"], df["基準日依據"], df["資料來源"] = h.trade_day, asof, basis, h.source
    return store.normalize_constituents(df)


def _latest_before(stored: pd.DataFrame, day: str) -> pd.DataFrame:
    """資料集裡早於 day 的最近一個交易日的成分列。"""
    earlier = stored.loc[stored["交易日"] < day, "交易日"]
    return stored[stored["交易日"] == earlier.max()] if len(earlier) else stored.iloc[0:0]


def _same_holdings(a: pd.DataFrame, b: pd.DataFrame) -> bool:
    """兩天的成分是否完全相同 (代碼、數量、權重)。權重隨股價每天變，全同代表網站還沒更新。"""
    if len(a) == 0 or len(a) != len(b):
        return False
    cols = ["類別", "成分代碼", "數量", "權重(%)"]
    key = lambda df: sorted(map(tuple, df[cols].astype("string").fillna("").to_numpy()))   # noqa: E731
    return key(a) == key(b)


def _universe(codes: str | None) -> pd.DataFrame:
    u = store.load_universe()
    if codes:
        want = [c.strip() for c in codes.split(",") if c.strip()]
        missing = sorted(set(want) - set(u["代號"]))
        if missing:
            raise SystemExit(f"不在追蹤清單 (pcf/universe.csv): {missing}")
        u = u[u["代號"].isin(want)]
    return u


def collect(root: Path | None = None, codes: str | None = None, since: str | None = None,
            until: str | None = None, dry_run: bool = False, sleep: float = SLEEP,
            now: datetime | None = None, sources: dict | None = None, verbose: bool = True) -> dict:
    now = now or datetime.now()
    today, today_str = now.date(), now.date().isoformat()
    same_day_ready = now.hour >= SAME_DAY_HOUR
    sources = sources or SOURCES
    u = _universe(codes)
    cal = store.calendar(root)
    trading = set(store.price_days())
    past, future = candidate_days(today, trading, since, until)
    cur = store.load_summary(root)
    s_have = {c: set(g["交易日"]) for c, g in cur[cur["狀態"] == store.HAS_DATA].groupby("代號")}
    stats = {"summary_new": 0, "constituent_days_new": 0, "queried": 0, "failed": [], "latest": {}}

    for issuer, group in u.groupby("投信", sort=False):
        if issuer not in sources:
            stats["failed"] += [f"{c}(無 {issuer} 來源)" for c in group["代號"]]
            continue
        src = sources[issuer]()
        new_rows: list[dict] = []
        try:
            for _, etf in group.iterrows():
                code = etf["代號"]
                frames: list[pd.DataFrame] = []
                try:
                    have_s = s_have.setdefault(code, set())
                    stored = store.load_constituents(code, root)
                    have_h = set(stored["交易日"].dropna().unique())

                    def fetch(day: str, want_s: bool, want_h: bool) -> bool:
                        """查一天；有新資料回 True。"""
                        got = False
                        if want_s:
                            s = src.summary(code, day)
                            if s and s.trade_day not in have_s:
                                new_rows.append(summary_row(etf, s, cal))
                                have_s.add(s.trade_day)
                                got = True
                        if want_h:
                            h = src.holdings(code, day)
                            if h and h.trade_day not in have_h:
                                frame = constituent_frame(etf, h, cal)
                                known = pd.concat([stored, *frames], ignore_index=True)     # 含本次剛抓到的
                                if (not src.holdings_forward and h.trade_day >= today_str
                                        and _same_holdings(frame, _latest_before(known, h.trade_day))):
                                    print(f"[pcf] {issuer} {code} {h.trade_day}: 成分與前一個已存日完全相同，"
                                          "視為尚未更新，本次不收", file=sys.stderr)
                                else:
                                    frames.append(frame)
                                    have_h.add(h.trade_day)
                                    got = True
                        stats["queried"] += 1
                        time.sleep(sleep)
                        return got

                    for day in past:
                        # 當天的資料: 公告日為鍵的隨時可查 (前一晚就公告了)；資料日為鍵的要等收盤後
                        open_s = day < today_str or src.summary_forward or same_day_ready
                        open_h = day < today_str or src.holdings_forward or same_day_ready
                        want_s, want_h = open_s and day not in have_s, open_h and day not in have_h
                        if want_s or want_h:
                            fetch(day, want_s, want_h)
                    if src.summary_forward or src.holdings_forward:
                        misses = 0
                        for day in future:                      # 找到下一個交易日的清單就停
                            if day in have_s and (day in have_h or not src.holdings_forward):
                                break
                            if fetch(day, src.summary_forward and day not in have_s,
                                     src.holdings_forward and day not in have_h):
                                break
                            misses += 1
                            if misses >= FORWARD_MISSES:
                                break
                    if have_s:
                        stats["latest"][code] = max(have_s)
                except Exception as e:                          # noqa: BLE001
                    stats["failed"].append(code)
                    print(f"[pcf][error] {issuer} {code}: {type(e).__name__}: {e}", file=sys.stderr)
                if frames:                                      # 中途失敗也把已抓到的日子存起來
                    n = len(frames)
                    if not dry_run:
                        n = store.upsert_constituents(code, pd.concat(frames, ignore_index=True), root)
                    stats["constituent_days_new"] += n
        finally:
            src.close()
        if new_rows:
            if not dry_run:
                store.upsert_summary(pd.DataFrame(new_rows), root)
            stats["summary_new"] += len(new_rows)
        if verbose:
            days = sorted({r["交易日"] for r in new_rows})
            print(f"[pcf] {issuer}: {len(group)} 檔, 申購贖回新增 {len(new_rows)} 列"
                  + (f" (交易日 {days[0]} ~ {days[-1]})" if days else ""))
    print(f"[pcf] 完成{' (dry-run)' if dry_run else ''}: 申購贖回新增 {stats['summary_new']} 列, "
          f"成分新增 {stats['constituent_days_new']} 檔日, 查詢 {stats['queried']} 次, 失敗 {len(stats['failed'])} 檔")
    return stats


# --------------------------------------------------------------------------- #
# 驗證: 重抓指定交易日，與資料集裡已有的列逐欄比對
# --------------------------------------------------------------------------- #
def _close(a, b, tol=1e-6) -> bool:
    if pd.isna(a) and pd.isna(b):
        return True
    if pd.isna(a) or pd.isna(b):
        return False
    return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(a)), abs(float(b)))


def _keyed(df: pd.DataFrame) -> dict:
    """{(類別, 成分代碼, 到期月份): [列 dict, …]}；同鍵多列 (同一檔債券分列) 依數量排序後逐列比。"""
    out: dict = {}
    for r in df.to_dict("records"):
        key = (r["類別"], r["成分代碼"], None if pd.isna(r["到期月份"]) else r["到期月份"])
        out.setdefault(key, []).append(r)
    for rows in out.values():
        rows.sort(key=lambda r: (-1.0 if pd.isna(r["數量"]) else float(r["數量"]),
                                 -1.0 if pd.isna(r["權重(%)"]) else float(r["權重(%)"])))
    return out


def verify(days: list[str], root: Path | None = None, codes: str | None = None, sleep: float = SLEEP) -> int:
    u = _universe(codes)
    cal = store.calendar(root)
    cur = store.load_summary(root)
    bad = 0
    for issuer, group in u.groupby("投信", sort=False):
        src = SOURCES[issuer]()
        ok_s = ok_h = n_s = n_h = 0
        try:
            for _, etf in group.iterrows():
                code = etf["代號"]
                old_c = store.load_constituents(code, root)
                for day in days:
                    try:
                        s, h = src.summary(code, day), src.holdings(code, day)
                    except Exception as e:                      # noqa: BLE001
                        bad += 1
                        print(f"[verify] {issuer} {code} {day}: 擷取失敗 {type(e).__name__}: {e}")
                        continue
                    time.sleep(sleep)
                    if s:
                        n_s += 1
                        old = cur[(cur["代號"] == code) & (cur["交易日"] == s.trade_day)]
                        new = summary_row(etf, s, cal)
                        diffs = [] if len(old) else ["資料集無此列"]
                        for col in store.SUMMARY_NUMERIC if len(old) else []:
                            if not _close(old.iloc[0][col], new[col]):
                                diffs.append(f"{col}: 資料集 {old.iloc[0][col]} vs 重抓 {new[col]}")
                        if len(old) and (old.iloc[0]["公告日期"] or None) != new["公告日期"]:
                            diffs.append(f"公告日期: 資料集 {old.iloc[0]['公告日期']} vs 重抓 {new['公告日期']}")
                        if len(old) and pd.notna(old.iloc[0]["資料基準日"]) and old.iloc[0]["資料基準日"] != new["資料基準日"]:
                            diffs.append(f"資料基準日: 資料集(推定) {old.iloc[0]['資料基準日']} vs 來源 {new['資料基準日']}")
                        ok_s += not diffs
                        if diffs:
                            bad += 1
                            print(f"[verify] 申購贖回 {issuer} {code} {s.trade_day}: " + "; ".join(diffs[:4]))
                    if h:
                        n_h += 1
                        a = _keyed(old_c[old_c["交易日"] == h.trade_day])
                        b = _keyed(constituent_frame(etf, h, cal))
                        diffs = []
                        if set(a) != set(b):
                            diffs.append(f"成分鍵不同: 只在資料集 {sorted(set(a) - set(b), key=str)[:3]} "
                                         f"只在重抓 {sorted(set(b) - set(a), key=str)[:3]} "
                                         f"(資料集 {sum(map(len, a.values()))} 列, 重抓 {sum(map(len, b.values()))} 列)")
                        for key in set(a) & set(b):
                            if len(a[key]) != len(b[key]):
                                diffs.append(f"{key} 列數 {len(a[key])} vs {len(b[key])}")
                                continue
                            for x, y in zip(a[key], b[key]):
                                for col in store.CONSTITUENT_NUMERIC:
                                    if not _close(x[col], y[col]):
                                        diffs.append(f"{key} {col}: 資料集 {x[col]} vs 重抓 {y[col]}")
                        if a and pd.notna(old_c[old_c["交易日"] == h.trade_day]["資料基準日"].iloc[0]):
                            o = old_c[old_c["交易日"] == h.trade_day]["資料基準日"].iloc[0]
                            n = constituent_frame(etf, h, cal)["資料基準日"].iloc[0]
                            if o != n:
                                diffs.append(f"資料基準日: 資料集(推定) {o} vs 來源 {n}")
                        ok_h += not diffs
                        if diffs:
                            bad += 1
                            print(f"[verify] 成分 {issuer} {code} {h.trade_day}: " + "; ".join(diffs[:4])
                                  + (f" …共 {len(diffs)} 處" if len(diffs) > 4 else ""))
        finally:
            src.close()
        print(f"[verify] {issuer}: 申購贖回 {ok_s}/{n_s} 一致, 成分 {ok_h}/{n_h} 一致")
    return bad


def _heartbeat(stats: dict, ok: bool):
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        from common import heartbeat
        latest = max(stats["latest"].values()) if stats["latest"] else ""
        heartbeat.write("etf_pcf", status="ok" if ok else "fail", exit_code=0 if ok else 1,
                        message=f"申購贖回 +{stats['summary_new']} 列, 成分 +{stats['constituent_days_new']} 檔日",
                        stats={"summary_new": stats["summary_new"],
                               "constituent_days_new": stats["constituent_days_new"],
                               "latest": latest, "failed": stats["failed"]})
    except Exception:                                    # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ETF 申購買回清單 (PCF) 每日收集器")
    ap.add_argument("--codes", help="只處理這些代號 (逗號分隔；須在 pcf/universe.csv 內)")
    ap.add_argument("--since", help="回補起日 YYYY-MM-DD (交易日)")
    ap.add_argument("--until", help="回補迄日 YYYY-MM-DD (預設今天)")
    ap.add_argument("--dry-run", action="store_true", help="只抓不寫")
    ap.add_argument("--verify", help="重抓這些交易日 (逗號分隔) 與資料集比對後結束，不寫入")
    ap.add_argument("--root", type=Path, default=None, help=f"資料集目錄 (預設 {store.PCF_DIR})")
    ap.add_argument("--sleep", type=float, default=SLEEP, help=f"每次查詢後的間隔秒數 (預設 {SLEEP})")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.verify:
        days = [d.strip() for d in args.verify.split(",") if d.strip()]
        return 1 if verify(days, args.root, args.codes, args.sleep) else 0

    stats = collect(args.root, args.codes, args.since, args.until, args.dry_run, args.sleep,
                    verbose=not args.quiet)
    ok = not stats["failed"]
    if not args.dry_run:
        _heartbeat(stats, ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
