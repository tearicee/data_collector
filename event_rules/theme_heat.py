#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
題材熱點 (已知題材的新聞爆量 × 概念股籃子相對強弱)
================================================================
使用者 2026-11-02：資安 (91APP 外洩 → 幣安被駭) 的訊號在盤前就存在 (標題層級資安新聞 3 倍於基準)，但系統只有個股層級的關注度，
沒有題材層級的訊號 → 做這支。

訊號：某題材在視窗內「標題層級」命中的新聞則數 ≥ COUNT_MIN，且 ≥ RATIO_MIN × 該題材近 BASE_DAYS 天的日中位數；
      另附籃子 (題材概念股.csv 的母題材聯集，上市櫃普通股) 前一交易日相對大盤的強弱與漲停成員。
      籃子超過 MAX_BASKET 檔的大題材 (半導體、金融/營建…) 太泛，不報。
高波動子集：使用者的假設是「題材共識只會反映在該題材的高波動股」→ 籃內 60 日波動度在全市場前 1/3 的成員另算，
      與全市場同波動分位的股票比 (不是跟全市場中位數比)。--backtest 檢驗這個假設與假訊號比例。

用法：python -m event_rules.theme_heat --slot pre     # 08:25：前一交易日 13:30 → 現在 (盤前)
      python -m event_rules.theme_heat --slot post    # 21:35：今天 00:00 → 現在 (晚間)
      python -m event_rules.theme_heat --backtest 2026-10-01 2026-10-31
輸出：Discord (經 push_discord.guarded_send，同題材同天同時段只送一次)；D:/mops/news/signals/題材熱點_YYYY-MM.parquet
"""
import argparse
import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from event_rules import rules as R  # noqa: E402

TAGGED = "/mnt/d/mops/news/tagged/新聞標籤_*.parquet"
PRICE = "/mnt/d/finmind_data/TaiwanStockPrice/*/TaiwanStockPrice_*.parquet"
STOCK_INFO = "/mnt/d/finmind_data/TaiwanStockInfo/TaiwanStockInfo.parquet"
SIGNAL_DIR = Path("/mnt/d/mops/news/signals")
import os
BASE_DAYS = 30
RATIO_MIN, COUNT_MIN, MAX_BASKET = float(os.environ.get("TH_RATIO", 3.0)), int(os.environ.get("TH_COUNT", 10)), int(os.environ.get("TH_BASKET", 80))   # 2026-10 回測：3/10/80 → 約 30 個題材日/月，當日高波動子集超額中位 +0.3~0.4%、勝率 6 成；隔日轉負
VOL_DAYS, HIGH_VOL_Q, LIMIT_UP = 60, 2 / 3, 9.5
MIN_BASKET = 5                                       # 集團題材 (鴻海 4 檔) 不是族群，不報
SPLIT_RE = r"(?:^|\|)%s(?:\||$)"


def _info():
    si = pd.read_parquet(STOCK_INFO).drop_duplicates("stock_id")
    ok = si["type"].isin(["twse", "tpex"]) & si["stock_id"].str.fullmatch(r"[1-9]\d{3}")
    return set(si.loc[ok, "stock_id"]), dict(zip(si["stock_id"], si["stock_name"]))


def load_tagged(start: str) -> pd.DataFrame:
    files = [f for f in sorted(glob.glob(TAGGED)) if f[-15:-8] >= start[:7]]
    tg = pd.concat(pd.read_parquet(f, columns=["發布時間", "標題", "title_themes", "個股代號", "來源"]) for f in files)
    tg = tg[tg["發布時間"] >= pd.Timestamp(start)].drop_duplicates("標題")
    return tg[tg["title_themes"].astype(str) != ""]


def load_prices(start: str, end: str, listed: set) -> pd.DataFrame:
    """日 × 股 的漲跌幅 (對參考價 = close − spread)，只留上市櫃普通股。"""
    frames = []
    for f in sorted(glob.glob(PRICE)):
        d = f[-18:-8]
        if start <= d <= end:
            p = pd.read_parquet(f, columns=["stock_id", "close", "spread"])
            p = p[p["stock_id"].isin(listed) & (p["close"] > p["spread"])]
            p["pct"] = p["spread"] / (p["close"] - p["spread"]) * 100
            p["date"] = pd.Timestamp(d)
            frames.append(p[["date", "stock_id", "pct"]])
    return pd.concat(frames).pivot_table(index="date", columns="stock_id", values="pct", aggfunc="last")


def baskets() -> dict:
    return {t: s for t, s in R.THEME_STOCKS.items() if R.THEME_PARENT.get(t, t) == t and MIN_BASKET <= len(s) <= MAX_BASKET}


def theme_daily_counts(tg: pd.DataFrame, themes) -> pd.DataFrame:
    day = tg["發布時間"].dt.normalize()
    return pd.DataFrame({t: tg["title_themes"].str.contains(SPLIT_RE % pd.Series([t]).str.replace(r"([\\/()+.*?])", r"\\\1", regex=True)[0])
                         .groupby(day).sum() for t in themes}).fillna(0).astype(int)


def _ratio(n: int, b: float, t0, t1) -> float:
    """視窗命中數 / (平日日基準 × 視窗天數)：連假後的視窗橫跨 3 天，不正規化會全部爆量。"""
    days = max((t1 - t0) / pd.Timedelta(days=1), 0.5)
    return n / (b * days) if b > 0 else (99.0 if n else 0.0)


def _base(daily: pd.DataFrame, base_day):
    base = daily[(daily.index < base_day) & (daily.index >= base_day - pd.Timedelta(days=BASE_DAYS))]
    return base[base.index.dayofweek < 5]              # 週末新聞少，只用平日算日中位數


def window_signals(tg: pd.DataFrame, daily: pd.DataFrame, t0, t1, base_day) -> list:
    """視窗 [t0, t1) 內各題材的命中數對基準 (base_day 之前 BASE_DAYS 天平日的日中位數，依視窗天數正規化)。"""
    w = tg[(tg["發布時間"] >= t0) & (tg["發布時間"] < t1)]
    base = _base(daily, base_day)
    out = []
    for t in daily.columns:
        rx = SPLIT_RE % pd.Series([t]).str.replace(r"([\\/()+.*?])", r"\\\1", regex=True)[0]
        hit = w[w["title_themes"].str.contains(rx)]
        n, b = len(hit), float(base[t].median()) if len(base) else 0.0
        ratio = _ratio(n, b, t0, t1)
        if n >= COUNT_MIN and ratio >= RATIO_MIN:
            heads = hit.sort_values("發布時間", ascending=False)["標題"].head(3).tolist()
            out.append({"theme": t, "count": n, "base": round(b, 1), "ratio": round(ratio, 1) if b else 99.0, "headlines": heads})
    return sorted(out, key=lambda x: -x["ratio"])


def basket_stats(pct: pd.DataFrame, day, members: list, names: dict) -> dict:
    """某日籃子相對大盤：全部中位數差、高波動子集 (60 日波動前 1/3) 對同分位市場的中位數差、漲停成員。"""
    if day not in pct.index:
        return {}
    r = pct.loc[day].dropna()
    hist = pct[pct.index < day].tail(VOL_DAYS)
    vol = hist.std().dropna() if len(hist) >= 20 else pd.Series(dtype=float)
    mem = [m for m in members if m in r.index]
    if not mem:
        return {}
    mk_med = r.median()
    out = {"n": len(mem), "basket_med": r[mem].median(), "mkt_med": mk_med, "excess": r[mem].median() - mk_med,
           "limit_up": [f"{names.get(m, m)}" for m in mem if r[m] >= LIMIT_UP],
           "top": [f"{names.get(m, m)}{r[m]:+.1f}" for m in sorted(mem, key=lambda m: -r[m])[:5]]}
    if len(vol):
        thr = vol.quantile(HIGH_VOL_Q)
        hv_all = [s for s in r.index if s in vol.index and vol[s] >= thr]
        lv_all = [s for s in r.index if s in vol.index and vol[s] < thr]
        hv = [m for m in mem if m in vol.index and vol[m] >= thr]
        lv = [m for m in mem if m in vol.index and vol[m] < thr]
        out.update({"n_hv": len(hv), "hv_excess": (r[hv].median() - r[hv_all].median()) if hv else np.nan,
                    "n_lv": len(lv), "lv_excess": (r[lv].median() - r[lv_all].median()) if lv else np.nan,
                    "hv_members": [names.get(m, m) for m in hv]})
    return out


def fmt_signal(sig: dict, prev: dict, prev_day) -> str:
    s = f"**{sig['theme']}**｜視窗 {sig['count']} 則，基準 {sig['base']}/日，{sig['ratio']} 倍"
    if prev:
        s += (f"\n  籃子 {prev['n']} 檔 {prev_day:%m/%d} 中位 {prev['basket_med']:+.1f}%（大盤 {prev['mkt_med']:+.1f}%）"
              + (f"；高波動 {prev['n_hv']} 檔相對同級 {prev['hv_excess']:+.1f}%" if prev.get("n_hv") else "")
              + (f"；漲停 {'、'.join(prev['limit_up'][:5])}" if prev["limit_up"] else "")
              + f"\n  強勢 {' '.join(prev['top'])}")
    for h in sig["headlines"]:
        s += f"\n  · {h[:50]}"
    return s


def run_slot(slot: str, dry: bool) -> int:
    listed, names = _info()
    now = pd.Timestamp.now()
    today = now.normalize()
    pct = load_prices((today - pd.Timedelta(days=120)).strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"), listed)
    prev_day = max(d for d in pct.index if d < today) if slot == "pre" else pct.index.max()
    t0 = prev_day + pd.Timedelta(hours=13, minutes=30) if slot == "pre" else today
    tg = load_tagged((today - pd.Timedelta(days=BASE_DAYS + 7)).strftime("%Y-%m-%d"))
    bk = baskets()
    daily = theme_daily_counts(tg, bk.keys())
    sigs = window_signals(tg, daily, t0, now, today)
    rows, lines = [], [f"**{now:%Y-%m-%d %H:%M} 題材熱點（{'盤前' if slot == 'pre' else '晚間'}，{t0:%m/%d %H:%M} 起 {len(sigs)} 個）**"]
    for sg in sigs:
        prev = basket_stats(pct, prev_day, bk[sg["theme"]], names)
        lines.append(fmt_signal(sg, prev, prev_day))
        rows.append({"day": today, "slot": slot, **{k: v for k, v in sg.items() if k != "headlines"},
                     "headlines": " | ".join(sg["headlines"]), "prev_day": prev_day, **{k: v for k, v in prev.items() if not isinstance(v, list)},
                     "limit_up": "、".join(prev.get("limit_up", [])), "hv_members": "、".join(prev.get("hv_members", []))})
    msg = "\n".join(lines)
    print(msg)
    if not sigs:
        return 0
    SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
    path = SIGNAL_DIR / f"題材熱點_{today:%Y-%m}.parquet"
    old = pd.read_parquet(path) if path.exists() else pd.DataFrame()
    new = pd.DataFrame(rows)
    pd.concat([old[~((old.get("day") == today) & (old.get("slot") == slot))] if len(old) else old, new]).to_parquet(path, index=False)
    if not dry:
        from event_rules import push_discord as P
        P.guarded_send([{"key": f"theme-{slot}-{today:%Y-%m-%d}", "time": now, "score": 10.0, "text": msg}])
    return 0


def backtest(start: str, end: str) -> int:
    """每個交易日 d：視窗 = 前一交易日 13:30 → d 09:00 (盤前就看得到的)，籃子報酬看 d 當天與 d+1。
    對照組 = 同一天沒有爆量 (ratio < 1.5) 的題材，看高波動子集的方法是否只在爆量時有超額。"""
    listed, names = _info()
    pct = load_prices((pd.Timestamp(start) - pd.Timedelta(days=120)).strftime("%Y-%m-%d"), (pd.Timestamp(end) + pd.Timedelta(days=7)).strftime("%Y-%m-%d"), listed)
    tg = load_tagged((pd.Timestamp(start) - pd.Timedelta(days=BASE_DAYS + 7)).strftime("%Y-%m-%d"))
    bk = baskets()
    daily = theme_daily_counts(tg, bk.keys())
    days = [d for d in pct.index if start <= d.strftime("%Y-%m-%d") <= end]
    rec = []
    for d in days:
        prev = max((x for x in pct.index if x < d), default=None)
        nxt = min((x for x in pct.index if x > d), default=None)
        if prev is None:
            continue
        t0, t1 = prev + pd.Timedelta(hours=13, minutes=30), d + pd.Timedelta(hours=9)
        w = tg[(tg["發布時間"] >= t0) & (tg["發布時間"] < t1)]
        base = _base(daily, d)
        for t in bk:
            rx = SPLIT_RE % pd.Series([t]).str.replace(r"([\\/()+.*?])", r"\\\1", regex=True)[0]
            n, b = int(w["title_themes"].str.contains(rx).sum()), float(base[t].median())
            ratio = _ratio(n, b, t0, t1)
            flag = n >= COUNT_MIN and ratio >= RATIO_MIN
            if not flag and ratio >= 1.5:
                continue                              # 中間地帶不當對照
            s0, s1 = basket_stats(pct, d, bk[t], names), (basket_stats(pct, nxt, bk[t], names) if nxt is not None else {})
            sp = basket_stats(pct, prev, bk[t], names)
            if not s0:
                continue
            rec.append({"day": d, "theme": t, "flag": flag, "count": n, "base": round(b, 1), "ratio": round(ratio, 1),
                        "prev_excess": sp.get("excess"), "prev_limit_up": len(sp.get("limit_up", [])),
                        "d0_excess": s0["excess"], "d0_hv_excess": s0.get("hv_excess"), "d0_lv_excess": s0.get("lv_excess"), "n_hv": s0.get("n_hv"),
                        "d1_excess": s1.get("excess"), "d1_hv_excess": s1.get("hv_excess"), "d1_lv_excess": s1.get("lv_excess"),
                        "limit_up": "、".join(s0["limit_up"][:4])})
    df = pd.DataFrame(rec)
    pd.set_option("display.width", 250)
    f = df[df["flag"]].sort_values(["day", "ratio"], ascending=[True, False])
    print(f"== 爆量題材日 {len(f)} 個 (對照組 {len(df) - len(f)} 個題材日)")
    print(f[["day", "theme", "count", "base", "ratio", "prev_excess", "prev_limit_up", "d0_excess", "d0_hv_excess", "n_hv", "d0_lv_excess", "d1_excess", "d1_hv_excess", "limit_up"]]
          .assign(day=lambda x: x["day"].dt.strftime("%m-%d")).round(2).to_string(index=False))
    print("\n== 彙總 (中位數 / 勝率 = 超額 > 0 的比例)")
    for name, g in (("爆量", df[df["flag"]]), ("對照", df[~df["flag"]]), ("爆量且前一日籃子已強勢(超額>0 或有漲停)", df[df["flag"] & ((df["prev_excess"] > 0) | (df["prev_limit_up"] > 0))])):
        if not len(g):
            continue
        row = [f"{name} n={len(g)}"]
        for c in ("d0_excess", "d0_hv_excess", "d0_lv_excess", "d1_excess", "d1_hv_excess", "d1_lv_excess"):
            v = g[c].dropna()
            row.append(f"{c} 中位 {v.median():+.2f}% 勝率 {(v > 0).mean():.0%}" if len(v) else f"{c} —")
        print("  " + "；".join(row))
    SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(SIGNAL_DIR / f"題材熱點回測_{start}_{end}.parquet", index=False)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", choices=["pre", "post"])
    ap.add_argument("--backtest", nargs=2, metavar=("START", "END"))
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    if a.backtest:
        sys.exit(backtest(*a.backtest))
    sys.exit(run_slot(a.slot or "pre", a.dry))
