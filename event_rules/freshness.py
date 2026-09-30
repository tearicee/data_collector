#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新鮮度評分 v1 (本地規則，不經 AI 模型)
================================================================
把「重訊事件」與「新聞標籤」併成一張事件表，對每則事件算：

  稀有度 (依事件類型，而非只看同公司)：
    market_combo_days  過去 LOOKBACK 天內，「台股個股」出現相同「事件標籤 × 對象類別」的天數
                       例：可轉債 × 美系巨頭 —— 過去一年台股沒有外資巨頭認購 CB → 0 → 高新鮮度
                       只計「標題命中的標籤」且「有台股代號」的事件，避免美股新聞與內文順帶一提稀釋稀有度
    market_tag_days    過去 LOOKBACK 天內，全市場出現相同事件標籤的天數 (此類事件本身常不常見)
    stock_tag_days     過去 LOOKBACK 天內，同一檔個股出現相同事件標籤的天數
  加分：首創/極端/轉折/意外 用語、重量級對象、金額
  扣分：傳聞用語、例行/重發 (重訊)

score 0~10；reasons 欄列出每一項加減分，方便調整權重 (改 WEIGHTS 即可)。
歷史不足 HISTORY_MIN_DAYS 天的事件標 history_ok=False (稀有度不可靠)。

輸出：/mnt/d/mops/news/scored/新鮮度_YYYY-MM.parquet
     + /mnt/d/mops/news/review/新鮮度檢視_YYYY-MM-DD.csv (每日前 N 名，Excel 可開；
       「我的評價」欄留白供人工標記 該高分/雜訊，回饋用來調規則)
用法：python -m event_rules.freshness [--start 2026-09-01] [--top 30] [--review-days 3]
      未給 --start 時從上個月 1 日起算 (月檔整月覆寫，start 必須是月初)
"""
import argparse
import bisect
import glob
import json
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

MOPS_EVENTS = "/mnt/d/mops/material_info/derived/重訊事件_*.parquet"
NEWS_TAGGED = "/mnt/d/mops/news/tagged/新聞標籤_*.parquet"
OUT_DIR = Path("/mnt/d/mops/news/scored")
REVIEW_DIR = Path("/mnt/d/mops/news/review")
REVIEW_TOP = 60
LOOKBACK = 365
HISTORY_MIN_DAYS = 180

# 低資訊量標籤：不參與稀有度計算、也不單獨構成候選
LOW_TAGS = {"法說會", "人事異動", "金融投資", "背書保證/資金貸與", "股東會", "供應鏈連動", "機構評等", "財報", "營收"}
WEIGHTS = {
    "base": 2.0,
    "combo_never": 3.0, "combo_rare": 1.5,        # 標籤×對象類別：0 天 / ≤3 天
    "tag_rare_market": 1.0,                        # 該事件類型全市場 ≤12 天/年
    "stock_first": 1.0,                            # 該個股一年內首次出現此類事件
    "mod_首創": 1.5, "mod_極端": 1.5, "mod_轉折": 1.0, "mod_意外": 0.5, "mod_傳聞": -1.0,
    "entity": 1.0,                                 # 有重量級對象
    "amount_big": 1.0,                             # 金額 ≥ 50 億元 (重訊)
    "routine": -3.0, "mops_source": 0.5,           # 例行公告扣分；公司正式公告比報導可靠
}
COMBO_RARE_MAX, TAG_RARE_MAX, AMOUNT_BIG = 3, 12, 5e9
# 彙整型文章 (盤前/盤後/週報/懶人包) 一篇帶十幾個主題，標籤與對象都不可靠 → 不評分也不計入歷史
ROUNDUP_RE = r"盤後|盤前|盤中|大事回顧|一周|一週|周報|週報|懶人包|要聞|焦點股|早報|晚報|速報|優分析|操盤|本周|本週|下周|下週|族群重點"
FX = {"TWD": 1, "USD": 32, "CNY": 4.4, "JPY": 0.21, "EUR": 35, "HKD": 4.1}


def load_events() -> pd.DataFrame:
    parts = []
    files = sorted(glob.glob(MOPS_EVENTS))
    if files:
        m = pd.concat(pd.read_parquet(f) for f in files)
        m = m[m["filter_label"] != "repeat"]
        parts.append(pd.DataFrame({
            "source": "重訊", "id": m["MOPS鍵"], "time": m["發布時間"], "title": m["主旨"],
            "stocks": m["公司代號"], "tags": m["tags"], "title_tags": m["tags"], "modifiers": m["modifiers"], "entities": m["entities"],
            "routine": m["filter_label"] == "routine",
            "amount_twd": m["金額_元"] * m["幣別"].map(FX).fillna(1),
        }))
    files = sorted(glob.glob(NEWS_TAGGED))
    if files:
        n = pd.concat(pd.read_parquet(f) for f in files)
        parts.append(pd.DataFrame({
            "source": n["來源"], "id": n["連結"], "time": n["發布時間"], "title": n["標題"],
            "stocks": n["個股代號"], "tags": n["tags"], "title_tags": n["title_tags"], "modifiers": n["modifiers"], "entities": n["entities"],
            "routine": False, "amount_twd": float("nan"),
        }))
    ev = pd.concat(parts, ignore_index=True).sort_values("time").reset_index(drop=True)
    ev = ev[~ev["title"].str.contains(ROUNDUP_RE, na=False)].reset_index(drop=True)
    ev["day"] = ev["time"].dt.normalize()
    ev["tag_list"] = ev["tags"].map(lambda s: [t for t in s.split("|") if t and t not in LOW_TAGS])
    # 對象類別：新聞只認「標題裡出現」的 (內文提到不算主角)；重訊排除台系龍頭 (多半是公司自己)
    def ents(src, title, s):
        if not s:
            return []
        d = json.loads(s)
        if src == "重訊":
            return sorted(k for k in d if k != "台系龍頭")
        return sorted(k for k, names in d.items() if any(n in title for n in names))
    ev["ent_list"] = [ents(a, b or "", c) for a, b, c in zip(ev["source"], ev["title"], ev["entities"])]
    ev["stock_list"] = ev["stocks"].map(lambda s: [c for c in str(s).split(",") if c])
    ev["ttag_list"] = ev["title_tags"].map(lambda s: [t for t in s.split("|") if t and t not in LOW_TAGS])
    ev["tw"] = ev["stock_list"].map(lambda l: any(c[:4].isdigit() for c in l))
    return ev


def _index(ev: pd.DataFrame) -> dict:
    """key → 已排序的「出現日」(day ordinal) 清單。key 有三種：tag、(tag,對象類別)、(個股,tag)。"""
    days = defaultdict(set)
    for d, tags, ttags, ents, stocks, tw in zip(ev["day"], ev["tag_list"], ev["ttag_list"], ev["ent_list"],
                                                ev["stock_list"], ev["tw"]):
        o = d.toordinal()
        for t in tags:
            for s in stocks:
                days[("S", s, t)].add(o)
        if tw:  # 稀有度只看台股事件、且標籤須出現在標題/主旨
            for t in ttags:
                days[t].add(o)
                for e in ents:
                    days[(t, e)].add(o)
    return {k: sorted(v) for k, v in days.items()}


def _prior(idx: dict, key, o: int) -> int:
    lst = idx.get(key, [])
    return bisect.bisect_left(lst, o) - bisect.bisect_left(lst, o - LOOKBACK)


def score(ev: pd.DataFrame, start: str) -> pd.DataFrame:
    idx = _index(ev)
    first_day = ev["day"].min().toordinal()
    W = WEIGHTS
    out = []
    for r in ev[ev["time"] >= pd.Timestamp(start)].itertuples(index=False):
        if not r.tag_list:
            continue
        o = r.day.toordinal()
        s, why = W["base"], []
        ttags = r.ttag_list if r.tw else []
        combos = [(t, e, _prior(idx, (t, e), o)) for t in ttags for e in r.ent_list]
        tag_days = {t: _prior(idx, t, o) for t in (ttags or r.tag_list)}
        stock_days = [_prior(idx, ("S", c, t), o) for c in r.stock_list for t in r.tag_list]
        if combos:
            t, e, n = min(combos, key=lambda x: x[2])
            if n == 0:
                s += W["combo_never"]; why.append(f"「{t}×{e}」過去一年未出現 +{W['combo_never']}")
            elif n <= COMBO_RARE_MAX:
                s += W["combo_rare"]; why.append(f"「{t}×{e}」過去一年僅 {n} 天 +{W['combo_rare']}")
            s += W["entity"]; why.append(f"重量級對象 {'/'.join(r.ent_list)} +{W['entity']}")
        t, n = min(tag_days.items(), key=lambda x: x[1])
        if n <= TAG_RARE_MAX:
            s += W["tag_rare_market"]; why.append(f"「{t}」全市場過去一年僅 {n} 天 +{W['tag_rare_market']}")
        if stock_days and min(stock_days) == 0:
            s += W["stock_first"]; why.append(f"該個股一年內首見此類事件 +{W['stock_first']}")
        for m in [x for x in r.modifiers.split("|") if x]:
            s += W[f"mod_{m}"]; why.append(f"{m}用語 {W[f'mod_{m}']:+}")
        if r.amount_twd == r.amount_twd and r.amount_twd >= AMOUNT_BIG:
            s += W["amount_big"]; why.append(f"金額約 {r.amount_twd / 1e8:,.0f} 億 +{W['amount_big']}")
        if r.source == "重訊":
            s += W["mops_source"]
            if r.routine:
                s += W["routine"]; why.append(f"例行公告 {W['routine']}")
        out.append({
            "time": r.time, "source": r.source, "stocks": r.stocks, "title": r.title, "tags": "|".join(r.tag_list),
            "modifiers": r.modifiers, "entities": "|".join(r.ent_list), "score": round(max(0, min(10, s)), 1),
            "reasons": "；".join(why), "min_combo_days": min((c[2] for c in combos), default=None),
            "min_tag_days": n, "history_ok": o - first_day >= HISTORY_MIN_DAYS, "id": r.id,
        })
    return pd.DataFrame(out)


def write_review(sc: pd.DataFrame, days: int) -> list:
    """每日檢視表：當日分數前 REVIEW_TOP 名 (同標題只留最高分)。已存在且有人填過評價的檔不覆寫。"""
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    last = sc["time"].max().normalize()
    for i in range(days):
        day = last - pd.Timedelta(days=i)
        g = sc[sc["time"].dt.normalize() == day]
        if g.empty:
            continue
        path = REVIEW_DIR / f"新鮮度檢視_{day.date()}.csv"
        if path.exists():
            old = pd.read_csv(path, dtype=str, keep_default_na=False)
            if "我的評價" in old and (old["我的評價"].str.strip() != "").any():
                continue
        g = g.sort_values("score", ascending=False).drop_duplicates("title").head(REVIEW_TOP)
        out = pd.DataFrame({
            "分數": g["score"], "我的評價": "", "時間": g["time"].dt.strftime("%m-%d %H:%M"), "來源": g["source"],
            "個股": g["stocks"].str[:40], "標題": g["title"], "事件標籤": g["tags"], "用語": g["modifiers"],
            "對象": g["entities"], "評分理由": g["reasons"], "連結或MOPS鍵": g["id"],
        })
        out.to_csv(path, index=False, encoding="utf-8-sig")
        written.append(path.name)
    return written


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--review-days", type=int, default=0, help="輸出最近 N 天的每日檢視 CSV")
    a = ap.parse_args()
    if not a.start:
        first = pd.Timestamp.today().normalize().replace(day=1)
        a.start = str((first - pd.Timedelta(days=1)).replace(day=1).date())
    ev = load_events()
    sc = score(ev, a.start)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for ym, g in sc.groupby(sc["time"].dt.strftime("%Y-%m")):
        tmp = OUT_DIR / f"新鮮度_{ym}.parquet.tmp"
        g.to_parquet(tmp, compression="zstd", index=False)
        tmp.replace(OUT_DIR / f"新鮮度_{ym}.parquet")
    if a.review_days:
        print("檢視表：", write_review(sc, a.review_days))
    print(f"事件表 {len(ev):,} 筆 ({ev['day'].min().date()}~{ev['day'].max().date()})；評分 {len(sc):,} 筆")
    print(sc["score"].round().value_counts().sort_index().to_string())
    with pd.option_context("display.width", 250, "display.max_colwidth", 46, "display.unicode.east_asian_width", True):
        top = sc.sort_values("score", ascending=False).drop_duplicates("title").head(a.top)
        print(top[["time", "source", "stocks", "title", "score", "tags", "entities"]].to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
