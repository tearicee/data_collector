#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
事件因子表與事後報酬回填 (新聞因子研究的基底)
================================================================
每個「事件 × 主角個股」一列，存：
  因子 (事件當下就知道)：event_score、heat、各加分項 (稀有度/題材新穎/跨界/用語/對象/籌資/自結/澄清)、
        媒體家數、來源類型、方向、題材、標籤、發布到反應日的時間差、事前 5 日報酬 (是否已先反應)
  結果 (之後回填)：反應日當日、+1、+5、+10、+20 日報酬，以及相對全市場中位數的超額報酬，最大漲幅/最大回檔
輸出：/mnt/d/mops/news/signals/事件報酬_YYYY-MM.parquet (依事件發生月)
用法：python -m event_rules.event_returns            # 更新最近兩個月 (重算因子、回填已到期的報酬)
"""
import glob
import re
import sys
from pathlib import Path

import pandas as pd

SCORED = "/mnt/d/mops/news/scored/新鮮度_*.parquet"
PRICE = "/mnt/d/finmind_data/TaiwanStockPrice/*/TaiwanStockPrice_*.parquet"
OUT = Path("/mnt/d/mops/news/signals")
HORIZONS = (1, 5, 10, 20)
INDUSTRY = "/mnt/d/mops/MopsIndustry/*/MopsIndustry_*.parquet"
CTRL_QUIET_DAYS, CTRL_MAX = 5, 30   # 對照組：同產業、事件日前後 5 天都沒有任何事件的個股，最多取 30 檔
REASON_KEYS = {  # 加分項 → 因子欄位 (0/1 或數值)
    "f_combo_rare": r"過去一年(未出現|僅)", "f_theme_first": r"(首次|天前才開始)與「", "f_cross": r"跨界：", "f_stock_first": r"該個股一年內首見",
    "f_mod_first": r"首創用語", "f_mod_extreme": r"極端用語", "f_mod_turn": r"轉折用語", "f_mod_surprise": r"意外用語",
    "f_mod_rumor": r"傳聞用語", "f_mod_supply": r"供應鏈變數用語", "f_entity": r"重量級對象", "f_amount": r"金額約",
    "f_raise": r"(現金增資|私募|可轉債|公司債)決議", "f_selfreport": r"自結較", "f_clarify_deny": r"公司否認", "f_analyst": r"分析師本人發文",
    "f_routine": r"例行公告",
}
EXTRA_COLS = {"f_stale": lambda r: int(bool(getattr(r, "stale", ""))), "rank_pct": lambda r: getattr(r, "rank_pct", None)}


def main() -> int:
    files = sorted(glob.glob(SCORED))[-2:]
    sc = pd.concat(pd.read_parquet(f) for f in files)
    sc = sc[sc["stocks"].astype(str).str.match(r"^\d{4}")].copy()
    sc["stock"] = sc["stocks"].astype(str).str.split(",").str[0]
    px = pd.concat(pd.read_parquet(f, columns=["date", "stock_id", "close"]) for f in sorted(glob.glob(PRICE))[-120:])
    px["date"] = pd.to_datetime(px["date"])
    closes = px.pivot(index="date", columns="stock_id", values="close").sort_index()
    mkt = closes.pct_change().median(axis=1)             # 全市場中位數日報酬 (超額報酬基準)
    days = list(closes.index)
    ind = pd.read_parquet(sorted(glob.glob(INDUSTRY))[-1])
    industry = dict(zip(ind["stock_id"].astype(str), ind["category"]))
    by_ind = {}
    for c, cat in industry.items():
        by_ind.setdefault(cat, []).append(c)
    ev_days = {}   # 個股 → 有事件的日期 ordinal 集合 (用全部評分事件，不只主角)
    for t, stocks in zip(sc["time"], sc["stocks"]):
        for c in str(stocks).split(","):
            if c[:4].isdigit():
                ev_days.setdefault(c, set()).add(t.normalize().toordinal())
    ctrl_cache = {}

    def control_return(stock, i, h):
        """同產業、事件日 ±CTRL_QUIET_DAYS 天無事件的個股，h 日平均報酬 (%)。"""
        d = days[i]
        key = (industry.get(stock, ""), d, h)
        if key in ctrl_cache:
            return ctrl_cache[key]
        o = d.toordinal()
        peers = [c for c in by_ind.get(key[0], []) if c != stock and c in closes.columns
                 and not any(o - CTRL_QUIET_DAYS <= x <= o + CTRL_QUIET_DAYS for x in ev_days.get(c, ()))][:CTRL_MAX]
        rets = []
        for c in peers:
            s = closes[c]
            b = s.iloc[i - 1] if i > 0 else None
            if b and not pd.isna(b) and i + h < len(days) and not pd.isna(s.iloc[i + h]):
                rets.append((s.iloc[i + h] / b - 1) * 100)
        ctrl_cache[key] = (round(sum(rets) / len(rets), 2), len(rets)) if rets else (None, 0)
        return ctrl_cache[key]
    rows = []
    for r in sc.itertuples(index=False):
        d = r.reaction_day
        if pd.isna(d) or d not in closes.index or r.stock not in closes.columns:
            continue
        s = closes[r.stock]
        i = days.index(d)
        base = s.iloc[i - 1] if i > 0 else None   # 反應日前一日收盤
        if not base or pd.isna(base):
            continue
        row = {"event_id": r.event_id, "id": r.id, "time": r.time, "stock": r.stock, "source": r.source, "title": r.title,
               "reaction_day": d, "event_score": r.event_score, "market_score": r.market_score, "heat": getattr(r, "heat", None),
               "direction": r.direction, "tags": r.tags, "themes": r.themes, "n_sources": r.n_sources,
               "src_type": "重訊" if r.source == "重訊" else ("分析師" if str(r.source).startswith("分析師") else "新聞"),
               "hours_to_reaction": round((d + pd.Timedelta(hours=9) - r.time).total_seconds() / 3600, 1),
               "pre5_ret": round((base / s.iloc[i - 6] - 1) * 100, 2) if i >= 6 and s.iloc[i - 6] else None}
        for k, pat in REASON_KEYS.items():
            row[k] = int(bool(re.search(pat, str(r.reasons))))
        for k, fn in EXTRA_COLS.items():
            row[k] = fn(r)
        row["ret_d0"] = round((s.iloc[i] / base - 1) * 100, 2)
        row["abn_d0"] = round(row["ret_d0"] - mkt.iloc[i] * 100, 2)
        for h in HORIZONS:
            if i + h < len(days) and not pd.isna(s.iloc[i + h]):
                row[f"ret_{h}d"] = round((s.iloc[i + h] / base - 1) * 100, 2)
                row[f"abn_{h}d"] = round(row[f"ret_{h}d"] - (mkt.iloc[i:i + h + 1].add(1).prod() - 1) * 100, 2)
                win = s.iloc[i:i + h + 1]
                row[f"maxup_{h}d"], row[f"maxdn_{h}d"] = round((win.max() / base - 1) * 100, 2), round((win.min() / base - 1) * 100, 2)
                cr, n_ctrl = control_return(r.stock, i, h)
                row[f"ctrl_{h}d"], row[f"n_ctrl_{h}d"] = cr, n_ctrl
                row[f"abnctrl_{h}d"] = round(row[f"ret_{h}d"] - cr, 2) if cr is not None else None
            else:
                row[f"ret_{h}d"] = row[f"abn_{h}d"] = row[f"maxup_{h}d"] = row[f"maxdn_{h}d"] = None
                row[f"ctrl_{h}d"] = row[f"n_ctrl_{h}d"] = row[f"abnctrl_{h}d"] = None
        rows.append(row)
    out = pd.DataFrame(rows)
    OUT.mkdir(parents=True, exist_ok=True)
    for ym, g in out.groupby(out["time"].dt.strftime("%Y-%m")):
        g.to_parquet(OUT / f"事件報酬_{ym}.parquet", index=False)
    done = out.dropna(subset=["ret_5d"])
    print(f"事件×個股 {len(out):,} 列；已有 5 日報酬 {len(done):,} 列")
    if len(done) > 30:
        done["bucket"] = pd.cut(done["event_score"], [0, 4, 6, 7, 8, 11], labels=["≤4", "4-6", "6-7", "7-8", "8+"])
        print(done.groupby("bucket", observed=True).agg(n=("abn_5d", "size"), abn_d0=("abn_d0", "mean"), abn_5d=("abn_5d", "mean"),
                                                     vs同業無新聞_5d=("abnctrl_5d", "mean"), 勝率5d=("abnctrl_5d", lambda x: (x > 0).mean())).round(2).to_string())
        st = done[done["f_stale"] == 1] if "f_stale" in done else done.iloc[0:0]
        if len(st):
            print(f"舊聞 {len(st)} 筆：5 日對同業無新聞 {st['abnctrl_5d'].mean():+.2f}% (非舊聞 {done[done['f_stale'] == 0]['abnctrl_5d'].mean():+.2f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
