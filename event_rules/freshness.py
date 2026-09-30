#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新鮮度評分 v2 (本地規則，不經 AI 模型) — 兩階段
================================================================
把「重訊事件」與「新聞標籤」併成一張事件表，每則算兩個分數：

【第一階段 事件分 event_score】新聞/重訊一進來就能算
  稀有度
    標籤×對象     過去一年「台股個股」出現相同「事件標籤 × 對象類別」的天數 (輝達認購聯發科 ECB)
    個股×題材     這檔個股過去一年是否曾與該題材一起出現 (首次共現)
    產業×題材     該題材的新聞裡，這個產業的個股占比極低 → 跨界 (鋼鐵廠切入半導體)
    個股×標籤     這檔個股一年內首次出現此類事件
  內容
    用語          首創 / 極端 / 轉折 / 意外 (+)、傳聞 (-)
    重量級對象、金額 (重訊)
    澄清立場      公司「否認」題材且股價先前已大漲 → 高分、方向偏空；制式否認不加分
    自結數字      與「最近一期」比：同公司上月自結有公布 → 營收/獲利/EPS 月增率；
                  沒有 → 單月×3 對上一季。變動 ≥20% 加分、≥50% 再加 (年增率只列參考)。
                  仍虧損但股價續漲另外標註。只評重訊，新聞的自結報導不另評分。
  事件合併        同一事件的多篇報導給同一個 event_id (見 assign_events)，檢視/推播只出現一次
  扣分            例行公告

【第二階段 市場分 market_score】反應日收盤後才有 (13:30 前的事件看當日，之後看下一交易日)
    個股反應      事件個股當日漲跌幅 (漲跌停 / ≥5%)
    題材反應      題材概念股 (過去半年最常與該題材一起出現的個股) 當日漲跌幅中位數

score = min(10, event_score + market_score)；stage 欄標明「事件」或「含市場反應」。
每一項加減分都寫在 reasons / market_reason，權重集中在 WEIGHTS。

輸出：/mnt/d/mops/news/scored/新鮮度_YYYY-MM.parquet
     /mnt/d/mops/news/review/新鮮度檢視_YYYY-MM-DD.csv (每日前 N 名；「我的評價」欄供人工回饋)
用法：python -m event_rules.freshness [--start 2026-09-01] [--top 30] [--review-days 3]
      未給 --start 時從上個月 1 日起算 (月檔整月覆寫，start 必須是月初)
"""
import argparse
import bisect
import glob
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from event_rules import rules as R  # noqa: E402

MOPS_EVENTS = "/mnt/d/mops/material_info/derived/重訊事件_*.parquet"
NEWS_TAGGED = "/mnt/d/mops/news/tagged/新聞標籤_*.parquet"
PRICE_GLOB = "/mnt/d/finmind_data/TaiwanStockPrice/*/TaiwanStockPrice_*.parquet"
INDUSTRY_GLOB = "/mnt/d/mops/MopsIndustry/*/MopsIndustry_*.parquet"
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
    "theme_first": 1.5,                            # 個股×題材 首次共現
    "theme_cross": 3.0,                            # 產業×題材 罕見 (跨界)，與 theme_first 疊加
    "mod_首創": 1.5, "mod_極端": 1.5, "mod_轉折": 1.0, "mod_意外": 0.5, "mod_傳聞": -1.0,
    "entity": 1.0, "amount_big": 1.0, "routine": -3.0, "mops_source": 0.5,
    "clarify_deny_runup": 4.5, "clarify_deny": 1.0, "clarify_confirm": 1.5,
    "selfreport_chg": 1.5, "selfreport_chg_big": 2.5, "selfreport_loss_runup": 1.0, "multi_source": 0.5,
    "mkt_stock_limit": 2.0, "mkt_stock_big": 1.0, "mkt_theme_strong": 2.0, "mkt_theme_mild": 1.0, "mkt_cap": 3.0,
}
COMBO_RARE_MAX, TAG_RARE_MAX, AMOUNT_BIG = 3, 12, 5e9
CROSS_SHARE_MAX, CROSS_MIN_SAMPLE = 0.03, 30       # 產業在題材新聞中的占比 <3% 視為跨界 (題材樣本需 ≥30)
SELF_CHG, SELF_CHG_BIG = 20.0, 50.0                # 自結較最近一期變動幅度門檻 (%)
NEWS_SKIP_TAGS = {"自結"}                          # 這些標籤只評重訊；新聞若只有這些標籤則不評分
MERGE_HOURS, MERGE_SIM, MERGE_SIM_NUM = 36, 0.45, 0.25
RUNUP_DAYS, RUNUP_PCT = 5, 15.0                    # 澄清前 5 個交易日漲幅 ≥15%
NOVEL_GRACE_DAYS = 14                              # 題材新穎度寬限：近 14 天才開始共現的仍算「新題材」
FOCUS_MAX_STOCKS = 3                               # 題材新穎度只看「主角明確」的文章 (台股代號 ≤3 檔)
BASKET_DAYS, BASKET_SIZE, BASKET_MIN = 180, 15, 3
FX = {"TWD": 1, "USD": 32, "CNY": 4.4, "JPY": 0.21, "EUR": 35, "HKD": 4.1}
# 彙整型文章 (盤前/盤後/週報/懶人包) 一篇帶十幾個主題，標籤與對象都不可靠 → 不評分也不計入歷史
ROUNDUP_RE = r"盤後|盤前|盤中|大事回顧|一周|一週|周報|週報|懶人包|要聞|焦點股|早報|晚報|速報|優分析|操盤|本周|本週|下周|下週|族群重點|量大強漲|開盤|收盤|飆股出爐|排行榜|前\d+大|\d+大飆股|熱度爆棚"


def _split(s) -> list:
    return [x for x in str(s or "").split("|") if x]


def load_events() -> pd.DataFrame:
    parts = []
    files = sorted(glob.glob(MOPS_EVENTS))
    if files:
        m = pd.concat(pd.read_parquet(f) for f in files)
        m = m[m["filter_label"] != "repeat"]
        parts.append(pd.DataFrame({
            "source": "重訊", "id": m["MOPS鍵"], "time": m["發布時間"], "title": m["主旨"],
            "stocks": m["公司代號"], "tags": m["tags"], "title_tags": m["tags"], "modifiers": m["modifiers"],
            "entities": m["entities"], "themes": m["themes"], "title_themes": m["title_themes"],
            "routine": m["filter_label"] == "routine", "stance": m["澄清立場"], "data": m["數據"],
            "amount_twd": m["金額_元"] * m["幣別"].map(FX).fillna(1),
        }))
    files = sorted(glob.glob(NEWS_TAGGED))
    if files:
        n = pd.concat(pd.read_parquet(f) for f in files)
        parts.append(pd.DataFrame({
            "source": n["來源"], "id": n["連結"], "time": n["發布時間"], "title": n["標題"],
            "stocks": n["個股代號"], "tags": n["tags"], "title_tags": n["title_tags"], "modifiers": n["modifiers"],
            "entities": n["entities"], "themes": n["themes"], "title_themes": n["title_themes"],
            "routine": False, "stance": "", "data": "", "amount_twd": float("nan"),
        }))
    ev = pd.concat(parts, ignore_index=True).sort_values("time").reset_index(drop=True)
    ev = ev[~ev["title"].str.contains(ROUNDUP_RE, na=False)].reset_index(drop=True)
    ev["day"] = ev["time"].dt.normalize()
    ev["tag_list"] = ev["tags"].map(lambda s: [t for t in _split(s) if t not in LOW_TAGS])
    ev["ttag_list"] = ev["title_tags"].map(lambda s: [t for t in _split(s) if t not in LOW_TAGS])
    ev["theme_list"] = ev["themes"].map(_split)
    ev["ttheme_list"] = ev["title_themes"].map(_split)

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
    ev["tw_list"] = ev["stock_list"].map(lambda l: [c for c in l if c[:4].isdigit() and not c.startswith("00")])
    ev["tw"] = ev["tw_list"].map(bool)
    return ev


class Market:
    """股價 (漲跌幅、收盤) 與產業類別。"""

    def __init__(self, since: pd.Timestamp):
        files = [f for f in sorted(glob.glob(PRICE_GLOB)) if f[-18:-8] >= str((since - pd.Timedelta(days=20)).date())]
        px = pd.concat(pd.read_parquet(f, columns=["date", "stock_id", "close", "spread"]) for f in files)
        px["date"] = pd.to_datetime(px["date"])
        prev = px["close"] - px["spread"]
        px["pct"] = (px["spread"] / prev.where(prev > 0) * 100).round(2)
        self.pct = px.pivot_table(index="date", columns="stock_id", values="pct", aggfunc="last")
        self.close = px.pivot_table(index="date", columns="stock_id", values="close", aggfunc="last")
        self.days = list(self.pct.index)
        f = sorted(glob.glob(INDUSTRY_GLOB))
        ind = pd.read_parquet(f[-1]) if f else pd.DataFrame(columns=["stock_id", "category"])
        self.industry = dict(zip(ind["stock_id"].astype(str), ind["category"]))

    def reaction_day(self, t: pd.Timestamp):
        """13:30 前的事件 → 當日 (若為交易日)；否則下一個交易日。尚無資料回 None。"""
        d = t.normalize()
        i = bisect.bisect_left(self.days, d)
        if i < len(self.days) and self.days[i] == d and (t.hour, t.minute) < (13, 30):
            return d
        j = bisect.bisect_right(self.days, d)
        return self.days[j] if j < len(self.days) else None

    def pct_on(self, day, stock):
        try:
            v = self.pct.at[day, stock]
            return None if v != v else float(v)
        except KeyError:
            return None

    def runup(self, t: pd.Timestamp, stock: str):
        """事件當下為止 RUNUP_DAYS 個交易日的累計漲幅 (%)。"""
        if stock not in self.close.columns:
            return None
        cutoff = t.normalize() if (t.hour, t.minute) >= (13, 30) else t.normalize() - pd.Timedelta(days=1)
        s = self.close[stock].dropna()
        s = s[s.index <= cutoff]
        if len(s) <= RUNUP_DAYS:
            return None
        return round((s.iloc[-1] / s.iloc[-1 - RUNUP_DAYS] - 1) * 100, 1)


def _index(ev: pd.DataFrame, mk: Market):
    """key → 已排序的出現日。另回傳 題材→產業 計數 (跨界判斷) 與 題材→[(日, 個股)] (概念股籃)。"""
    days = defaultdict(set)
    theme_ind = defaultdict(Counter)
    theme_stock = defaultdict(list)
    for d, tags, ttags, ents, stocks, tw, themes, tthemes in zip(
            ev["day"], ev["tag_list"], ev["ttag_list"], ev["ent_list"], ev["stock_list"], ev["tw_list"],
            ev["theme_list"], ev["ttheme_list"]):
        o = d.toordinal()
        for t in tags:
            for s in stocks:
                days[("S", s, t)].add(o)
        for s in tw:
            for th in themes:                      # 個股×題材：內文提過也算「出現過」
                days[("T", s, th)].add(o)
        if tw:
            for t in ttags:                        # 標籤稀有度：只看台股事件、標籤須在標題/主旨
                days[t].add(o)
                for e in ents:
                    days[(t, e)].add(o)
            if len(tw) <= FOCUS_MAX_STOCKS:
                for th in tthemes:
                    for s in tw:
                        theme_ind[th][mk.industry.get(s, "")] += 1
                        theme_stock[th].append((o, s))
    return {k: sorted(v) for k, v in days.items()}, theme_ind, theme_stock


def _prior(idx: dict, key, o: int) -> int:
    lst = idx.get(key, [])
    return bisect.bisect_left(lst, o) - bisect.bisect_left(lst, o - LOOKBACK)


def _basket(theme_stock: dict, theme: str, o: int) -> list:
    if R.THEME_STOCKS.get(theme):                  # 使用者對照表優先
        return R.THEME_STOCKS[theme]
    c = Counter(s for d, s in theme_stock.get(theme, []) if o - BASKET_DAYS <= d < o)
    return [s for s, n in c.most_common(BASKET_SIZE) if n >= BASKET_MIN]


def _selfreports(ev: pd.DataFrame) -> dict:
    """(個股, 資料月份) → 自結數據，用來找「上個月的自結」。"""
    out = {}
    m = ev[(ev["source"] == "重訊") & ev["data"].str.contains('"自結"', na=False)]
    for stock, data in zip(m["stocks"], m["data"]):
        sr = json.loads(data).get("自結", {})
        if sr.get("資料月份"):
            out[(stock, sr["資料月份"])] = sr
    return out


def _self_change(sr: dict, prev: dict | None):
    """回傳 (基準說明, {項目: 變動%}, 單月EPS)。prev 有值用月增；否則單月×3 對上一季。"""
    items = {"營收": "營收", "母公司淨利": "獲利", "EPS": "EPS"}
    chg = {}
    for key, name in items.items():
        cur = sr.get(f"{key}_最近一月")
        base = prev.get(f"{key}_最近一月") if prev else (sr.get(f"{key}_最近一季") or 0) / 3
        if cur is None or not base or base <= 0:   # 基期為負/零時增減率無意義
            continue
        chg[name] = round((cur / base - 1) * 100, 1)
    return ("上月自結" if prev else "上一季月均"), chg, sr.get("EPS_最近一月")


def score(ev: pd.DataFrame, start: str, mk: Market) -> pd.DataFrame:
    idx, theme_ind, theme_stock = _index(ev, mk)
    selfs = _selfreports(ev)
    first_day = ev["day"].min().toordinal()
    W = WEIGHTS
    out = []
    for r in ev[ev["time"] >= pd.Timestamp(start)].itertuples(index=False):
        if not r.tag_list and not r.ttheme_list:
            continue
        if r.source != "重訊" and r.tag_list and set(r.tag_list) <= NEWS_SKIP_TAGS and not r.ttheme_list:
            continue
        o = r.day.toordinal()
        s, why, direction = W["base"], [], ""

        # ---- 稀有度：標籤×對象、標籤、個股×標籤
        ttags = r.ttag_list if r.tw else []
        combos = [(t, e, _prior(idx, (t, e), o)) for t in ttags for e in r.ent_list]
        if combos:
            t, e, n = min(combos, key=lambda x: x[2])
            if n == 0:
                s += W["combo_never"]; why.append(f"「{t}×{e}」過去一年未出現 +{W['combo_never']}")
            elif n <= COMBO_RARE_MAX:
                s += W["combo_rare"]; why.append(f"「{t}×{e}」過去一年僅 {n} 天 +{W['combo_rare']}")
        if r.ent_list:
            s += W["entity"]; why.append(f"重量級對象 {'/'.join(r.ent_list)} +{W['entity']}")
        tag_days = {t: _prior(idx, t, o) for t in (ttags or r.tag_list)}
        if tag_days:
            t, n = min(tag_days.items(), key=lambda x: x[1])
            if n <= TAG_RARE_MAX and r.tw:
                s += W["tag_rare_market"]; why.append(f"「{t}」全市場過去一年僅 {n} 天 +{W['tag_rare_market']}")
        stock_days = [_prior(idx, ("S", c, t), o) for c in r.stock_list for t in r.tag_list]
        if stock_days and min(stock_days) == 0:
            s += W["stock_first"]; why.append(f"該個股一年內首見此類事件 +{W['stock_first']}")

        # ---- 題材新穎度：個股×題材首次共現、產業×題材跨界 (主角明確的文章才算)
        if r.ttheme_list and 0 < len(r.tw_list) <= FOCUS_MAX_STOCKS:
            best = None
            for c in r.tw_list:
                ind = mk.industry.get(c, "")
                for th in r.ttheme_list:
                    if _prior(idx, ("T", c, th), o - NOVEL_GRACE_DAYS) > 0:   # 14 天前就出現過 → 不算新
                        continue
                    since = o - min((d for d in idx.get(("T", c, th), []) if d >= o - NOVEL_GRACE_DAYS), default=o)
                    total = sum(theme_ind[th].values())
                    share = theme_ind[th][ind] / total if total else 1
                    cross = bool(ind) and total >= CROSS_MIN_SAMPLE and share < CROSS_SHARE_MAX
                    if best is None or cross > best[3]:
                        best = (c, th, ind, cross, share, since)
            if best:
                c, th, ind, cross, share, since = best
                when = "首次" if since == 0 else f"{since} 天前才開始"
                s += W["theme_first"]; why.append(f"{c} {when}與「{th}」題材一起出現 +{W['theme_first']}")
                if cross:
                    s += W["theme_cross"]
                    why.append(f"跨界：{ind}在「{th}」新聞中僅占 {share:.1%} +{W['theme_cross']}")

        # ---- 用語、金額
        for m in _split(r.modifiers):
            s += W[f"mod_{m}"]; why.append(f"{m}用語 {W[f'mod_{m}']:+}")
        if r.amount_twd == r.amount_twd and r.amount_twd >= AMOUNT_BIG:
            s += W["amount_big"]; why.append(f"金額約 {r.amount_twd / 1e8:,.0f} 億 +{W['amount_big']}")

        # ---- 重訊專屬：澄清立場、自結數字、例行
        if r.source == "重訊":
            s += W["mops_source"]
            data = json.loads(r.data) if r.data else {}
            if r.stance == "否認":
                ru = mk.runup(r.time, r.stocks)
                if ru is not None and ru >= RUNUP_PCT:
                    s += W["clarify_deny_runup"]; direction = "空"
                    why.append(f"公司否認題材，且股價近 {RUNUP_DAYS} 日已漲 {ru}% +{W['clarify_deny_runup']}")
                else:
                    s += W["clarify_deny"]; why.append(f"公司否認報導 +{W['clarify_deny']}")
            elif r.stance == "證實":
                s += W["clarify_confirm"]; why.append(f"公司證實報導 +{W['clarify_confirm']}")
            sr = data.get("自結", {})
            if sr.get("資料月份"):
                ym = pd.Period(sr["資料月份"], "M")
                basis, chg, m_eps = _self_change(sr, selfs.get((r.stocks, str(ym - 1))))
                if chg and max(abs(x) for x in chg.values()) > 500:   # 多半是表格單位不一致造成的解析錯誤
                    why.append("(自結數字變動異常大，疑似解析問題，未計分)")
                elif chg:
                    k, v = max(chg.items(), key=lambda x: abs(x[1]))
                    detail = "、".join(f"{a}{b:+.0f}%" for a, b in chg.items())
                    if abs(v) >= SELF_CHG_BIG:
                        s += W["selfreport_chg_big"]; why.append(f"自結較{basis}：{detail} +{W['selfreport_chg_big']}")
                    elif abs(v) >= SELF_CHG:
                        s += W["selfreport_chg"]; why.append(f"自結較{basis}：{detail} +{W['selfreport_chg']}")
                    else:
                        why.append(f"(自結較{basis}：{detail}，變動不大)")
                    if not direction and abs(v) >= SELF_CHG:
                        direction = "多" if v > 0 else "空"
                yoy = sr.get("EPS_月年增_pct")
                if yoy is not None and abs(yoy) < 1e5:
                    why.append(f"(參考：EPS 年增 {yoy:.0f}%)")
                if m_eps is not None and m_eps < 0:
                    ru = mk.runup(r.time, r.stocks)
                    if ru is not None and ru >= RUNUP_PCT:
                        s += W["selfreport_loss_runup"]
                        why.append(f"單月仍虧損 (EPS {m_eps}) 但股價近 {RUNUP_DAYS} 日漲 {ru}% +{W['selfreport_loss_runup']}")
                    else:
                        why.append(f"(單月仍虧損 EPS {m_eps})")
            if r.routine:
                s += W["routine"]; why.append(f"例行公告 {W['routine']}")
        event_score = round(max(0, min(10, s)), 1)

        # ---- 第二階段：市場反應
        mscore, mwhy, rday = None, [], mk.reaction_day(r.time)
        if rday is not None:
            mscore = 0.0
            targets = r.tw_list[:5]
            via = ""
            if not targets:                        # 沒有台股代號 (外國公司事件) → 用對象概念股對應
                for ent in R.mapped_entities(r.title):
                    if R.ENTITY_STOCKS.get(ent):
                        targets, via = R.ENTITY_STOCKS[ent][:8], f"[{ent}概念股] "
                        break
            moves = [(c, mk.pct_on(rday, c)) for c in targets]
            moves = [(c, p) for c, p in moves if p is not None]
            if moves:
                c, p = max(moves, key=lambda x: abs(x[1]))
                if abs(p) >= 9.5:
                    mscore += W["mkt_stock_limit"]; mwhy.append(f"{via}{c} {p:+.1f}% (漲跌停) +{W['mkt_stock_limit']}")
                elif abs(p) >= 5:
                    mscore += W["mkt_stock_big"]; mwhy.append(f"{via}{c} {p:+.1f}% +{W['mkt_stock_big']}")
                if not direction and abs(p) >= 5:
                    direction = "多" if p > 0 else "空"
            best = None
            for th in r.ttheme_list:
                ps = [mk.pct_on(rday, c) for c in _basket(theme_stock, th, o)]
                ps = sorted(p for p in ps if p is not None)
                if len(ps) >= 5:
                    med = ps[len(ps) // 2]
                    if best is None or abs(med) > abs(best[1]):
                        best = (th, med, len(ps))
            if best:
                th, med, n = best
                if abs(med) >= 3:
                    mscore += W["mkt_theme_strong"]; mwhy.append(f"「{th}」概念股 {n} 檔中位數 {med:+.1f}% +{W['mkt_theme_strong']}")
                elif abs(med) >= 1.5:
                    mscore += W["mkt_theme_mild"]; mwhy.append(f"「{th}」概念股 {n} 檔中位數 {med:+.1f}% +{W['mkt_theme_mild']}")
                if not direction and abs(med) >= 1.5:
                    direction = "多" if med > 0 else "空"
            mscore = min(W["mkt_cap"], mscore)
        total = round(min(10, event_score + (mscore or 0)), 1)
        out.append({
            "time": r.time, "source": r.source, "stocks": r.stocks, "title": r.title,
            "score": total, "event_score": event_score, "market_score": mscore,
            "stage": "事件" if mscore is None else "含市場反應", "direction": direction,
            "tags": "|".join(r.tag_list), "themes": "|".join(r.ttheme_list), "modifiers": r.modifiers,
            "entities": "|".join(r.ent_list), "reasons": "；".join(why), "market_reason": "；".join(mwhy),
            "reaction_day": rday, "history_ok": o - first_day >= HISTORY_MIN_DAYS, "id": r.id,
        })
    return pd.DataFrame(out)


def _bigrams(s: str) -> set:
    s = "".join(ch for ch in s if ch.isalnum())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _numbers(s: str) -> set:
    import re
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*\.?\d*", s) if len(n.replace(",", "")) >= 2}


def assign_events(sc: pd.DataFrame) -> pd.DataFrame:
    """把同一事件的多篇報導併成一個 event_id (寧可少併不錯併)。條件須同時成立：
      1. 發布時間相差 ≤ MERGE_HOURS 小時
      2. 主角相同：有共同台股代號；都沒有台股代號時須有共同對象類別
      3. 標題相似：字元 bigram Jaccard ≥ MERGE_SIM；或兩邊標題有相同數字且相似度 ≥ MERGE_SIM_NUM
    重訊不與重訊合併 (每則公告都是獨立事件)。只加欄位不刪資料。"""
    sc = sc.sort_values("time").reset_index(drop=True)
    feats = []
    for r in sc.itertuples(index=False):
        tw = {c for c in str(r.stocks).split(",") if c[:4].isdigit()}
        feats.append((tw, set(_split(r.entities)), _bigrams(r.title), _numbers(r.title)))
    eid = list(range(len(sc)))
    window = pd.Timedelta(hours=MERGE_HOURS)
    times, srcs = sc["time"].tolist(), sc["source"].tolist()
    start = 0
    for i in range(len(sc)):
        while times[i] - times[start] > window:
            start += 1
        tw, ent, bg, num = feats[i]
        if not bg:
            continue
        best, best_sim = None, 0.0
        for j in range(start, i):
            if srcs[i] == "重訊" and srcs[j] == "重訊":
                continue
            tw2, ent2, bg2, num2 = feats[j]
            same_actor = bool(tw & tw2) if (tw or tw2) else bool(ent & ent2)
            if not same_actor or not bg2:
                continue
            sim = len(bg & bg2) / len(bg | bg2)
            if (sim >= MERGE_SIM or (num & num2 and sim >= MERGE_SIM_NUM)) and sim > best_sim:
                best, best_sim = j, sim
        if best is not None:
            eid[i] = eid[best]
    sc["event_id"] = [f"E{sc['time'].iloc[e]:%y%m%d}-{e}" for e in eid]
    g = sc.groupby("event_id")
    sc["n_reports"] = g["id"].transform("count")
    sc["n_sources"] = g["source"].transform("nunique")
    return sc


def write_review(sc: pd.DataFrame, days: int) -> list:
    """每日檢視表：當日分數前 REVIEW_TOP 名 (同標題只留最高分)。已有人填過評價的檔不覆寫。"""
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
        g = g.sort_values(["score", "event_score"], ascending=False).drop_duplicates("event_id").head(REVIEW_TOP)
        out = pd.DataFrame({
            "報導數": g["n_reports"], "媒體數": g["n_sources"],
            "總分": g["score"], "我的評價": "", "事件分": g["event_score"], "市場分": g["market_score"],
            "階段": g["stage"], "方向": g["direction"], "時間": g["time"].dt.strftime("%m-%d %H:%M"),
            "來源": g["source"], "個股": g["stocks"].str[:40], "標題": g["title"], "事件標籤": g["tags"],
            "題材": g["themes"], "用語": g["modifiers"], "對象": g["entities"], "事件分理由": g["reasons"],
            "市場分理由": g["market_reason"], "連結或MOPS鍵": g["id"],
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
    mk = Market(pd.Timestamp(a.start))
    sc = assign_events(score(ev, a.start, mk))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for ym, g in sc.groupby(sc["time"].dt.strftime("%Y-%m")):
        tmp = OUT_DIR / f"新鮮度_{ym}.parquet.tmp"
        g.to_parquet(tmp, compression="zstd", index=False)
        tmp.replace(OUT_DIR / f"新鮮度_{ym}.parquet")
    if a.review_days:
        print("檢視表：", write_review(sc, a.review_days))
    print(f"事件表 {len(ev):,} 筆 ({ev['day'].min().date()}~{ev['day'].max().date()})；評分 {len(sc):,} 筆；"
          f"股價至 {mk.days[-1].date()}")
    print(sc["score"].round().value_counts().sort_index().to_string())
    with pd.option_context("display.width", 250, "display.max_colwidth", 44, "display.unicode.east_asian_width", True):
        top = sc.sort_values("score", ascending=False).drop_duplicates("event_id").head(a.top)
        print(top[["time", "source", "stocks", "title", "score", "event_score", "market_score", "direction"]]
              .assign(stocks=top["stocks"].str[:14]).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
