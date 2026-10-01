#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
潛在題材通報 (傳產跨入半導體型態的擴散偵測)
================================================================
邏輯：
  1. 熱點：最近 HOT_DAYS 天內，評分理由含「跨界」(產業 × 題材罕見共現) 且 事件分 ≥ HOT_SCORE 或市場分 ≥ 2
     的事件 → 得到 (產業, 題材, 錨定股) 清單。例：鋼鐵工業 × 半導體，錨定股 大甲 2221、彰源 2030。
  2. 候選：與錨定股同產業、最近 QUIET_DAYS 天「沒有任何新聞/重訊」、但股價已在動：
       近 5 日漲幅 ≥ RET5 或 連續 2 日各漲 ≥ RET1，且 當日量比 (量 / 20 日均量) ≥ VOL_RATIO
  3. 通報並存檔 signals/潛在題材_YYYY-MM.parquet (狀態=待驗證)；--validate 回填 5/10 日後報酬與
     「之後有沒有出現同題材新聞」，用來檢驗這套邏輯的泛用性 (命中率、平均報酬)。
用法：python -m event_rules.latent_theme            # 收盤後偵測 + 推播摘要
      python -m event_rules.latent_theme --validate # 回填歷史訊號
"""
import argparse
import glob
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from event_rules import rules as R  # noqa: E402

SCORED = "/mnt/d/mops/news/scored/新鮮度_*.parquet"
TAGGED = "/mnt/d/mops/news/tagged/新聞標籤_*.parquet"
PRICE = "/mnt/d/finmind_data/TaiwanStockPrice/*/TaiwanStockPrice_*.parquet"
INDUSTRY = "/mnt/d/mops/MopsIndustry/*/MopsIndustry_*.parquet"
STOCK_INFO = "/mnt/d/finmind_data/TaiwanStockInfo/TaiwanStockInfo.parquet"
SIG_DIR = Path("/mnt/d/mops/news/signals")
HOT_DAYS, HOT_SCORE, QUIET_DAYS = 7, 8.0, 5
RET5, RET1, VOL_RATIO = 8.0, 3.0, 1.5
PRICE_DAYS = 70
EXCLUDE_IND = {"其他", ""}                                       # 「其他」不是真正的產業群
EXCLUDE_THEME = {"金融", "證券", "資產", "營建", "資安硬體", "軟體", "被動", "電商/零售", "高股息/ETF"}


def load_prices() -> pd.DataFrame:
    files = sorted(glob.glob(PRICE))[-PRICE_DAYS:]
    px = pd.concat(pd.read_parquet(f, columns=["date", "stock_id", "close", "Trading_Volume"]) for f in files)
    px["date"] = pd.to_datetime(px["date"])
    return px.sort_values(["stock_id", "date"])


def momentum(px: pd.DataFrame) -> pd.DataFrame:
    g = px.groupby("stock_id")
    out = px.copy()
    out["ret1"] = g["close"].pct_change() * 100
    out["ret5"] = g["close"].pct_change(5) * 100
    out["ret1_prev"] = out.groupby("stock_id")["ret1"].shift(1)
    out["vol20"] = g["Trading_Volume"].transform(lambda s: s.rolling(20, min_periods=10).mean().shift(1))
    out["vol_ratio"] = out["Trading_Volume"] / out["vol20"]
    last = out[out["date"] == out["date"].max()].set_index("stock_id")
    return last


def hot_pairs(sc: pd.DataFrame, today: pd.Timestamp) -> list:
    w = sc[(sc["time"] >= today - pd.Timedelta(days=HOT_DAYS)) & sc["reasons"].str.contains("跨界", na=False)
           & (sc["event_score"] >= HOT_SCORE) & (sc["market_score"].fillna(0) >= 2)]   # 新聞夠新鮮且股價已反應
    pairs = {}
    for r in w.itertuples():
        m = re.search(r"跨界：(.+?)在「(.+?)」", r.reasons)
        if not m:
            continue
        key = (m[1], m[2])
        if m[1] in EXCLUDE_IND or m[2] in EXCLUDE_THEME:
            continue
        for c in str(r.stocks).split(","):
            if c[:4].isdigit():
                pairs.setdefault(key, set()).add(c)
    return [(ind, th, sorted(v)) for (ind, th), v in pairs.items()]


def recent_news_stocks(today: pd.Timestamp) -> set:
    files = sorted(glob.glob(TAGGED))[-2:]
    tg = pd.concat(pd.read_parquet(f, columns=["發布時間", "個股代號"]) for f in files)
    tg = tg[tg["發布時間"] >= today - pd.Timedelta(days=QUIET_DAYS)]
    return {c for s in tg["個股代號"] for c in str(s).split(",") if c}


def detect(push: bool, dry: bool) -> int:
    sc = pd.concat(pd.read_parquet(f) for f in sorted(glob.glob(SCORED))[-2:])
    today = pd.Timestamp.now().normalize()
    pairs = hot_pairs(sc, today)
    if not pairs:
        print("最近沒有跨界熱點")
        return 0
    px = momentum(load_prices())
    ind = pd.read_parquet(sorted(glob.glob(INDUSTRY))[-1])
    ind_map = dict(zip(ind["stock_id"].astype(str), ind["category"]))
    info = pd.read_parquet(STOCK_INFO).sort_values("date").drop_duplicates("stock_id", keep="last")
    names = dict(zip(info["stock_id"], info["stock_name"]))
    noisy = recent_news_stocks(today) | {c for s in sc[sc["time"] >= today - pd.Timedelta(days=QUIET_DAYS)]["stocks"]
                                        for c in str(s).split(",") if c}
    rows = []
    for industry, theme, anchors in pairs:
        peers = [c for c, cat in ind_map.items() if cat == industry and c not in anchors and c in px.index]
        for c in peers:
            m = px.loc[c]
            if c in noisy or pd.isna(m["vol_ratio"]) or pd.isna(m["ret5"]) or abs(m["ret5"]) > 60:
                continue
            trig = (m["ret5"] >= RET5) or (m["ret1"] >= RET1 and m["ret1_prev"] >= RET1)
            if trig and m["vol_ratio"] >= VOL_RATIO:
                rows.append({"日期": today.date(), "題材": theme, "產業": industry, "錨定股": ",".join(anchors),
                             "候選": c, "名稱": names.get(c, ""), "近5日%": round(m["ret5"], 1), "當日%": round(m["ret1"], 1),
                             "前一日%": round(m["ret1_prev"], 1), "量比": round(m["vol_ratio"], 2), "收盤": m["close"],
                             "狀態": "待驗證", "5日後%": None, "10日後%": None, "之後出現同題材新聞": None})
    sig = pd.DataFrame(rows)
    print(f"熱點 {len(pairs)} 組：{[(i, t, a) for i, t, a in pairs]}；候選 {len(sig)} 檔")
    if sig.empty:
        return 0
    SIG_DIR.mkdir(parents=True, exist_ok=True)
    path = SIG_DIR / f"潛在題材_{today:%Y-%m}.parquet"
    old = pd.read_parquet(path) if path.exists() else pd.DataFrame()
    key = lambda d: d["日期"].astype(str) + d["候選"]
    if len(old):
        sig = sig[~key(sig).isin(set(key(old)))]
        sig = pd.concat([old, sig], ignore_index=True)
    sig.to_parquet(path, index=False)
    new = sig[sig["日期"].astype(str) == str(today.date())]
    lines = [f"**{today:%Y-%m-%d} 潛在題材通報**（熱點：" + "；".join(f"{i}×{t}（{','.join(a)}）" for i, t, a in pairs) + "）"]
    for r in new.sort_values("近5日%", ascending=False).head(10).itertuples():
        lines.append(f"{r.候選} {r.名稱}｜{r.產業}→{r.題材}｜近5日 {r._7:+.1f}%、今日 {r._8:+.1f}%、量比 {r.量比}｜尚無新聞")
    msg = "\n".join(lines)
    print(msg)
    if push and len(new):
        from event_rules import push_discord as pdc
        pdc.guarded_send([{"key": f"latent-{today.date()}", "time": pd.Timestamp.now(), "score": 10.0, "text": msg}], dry=dry)
    return 0


def validate() -> int:
    px = load_prices()
    closes = px.pivot(index="date", columns="stock_id", values="close")
    files = sorted(glob.glob(TAGGED))[-3:]
    tg = pd.concat(pd.read_parquet(f, columns=["發布時間", "個股代號", "themes"]) for f in files)
    for path in sorted(SIG_DIR.glob("潛在題材_*.parquet")):
        sig = pd.read_parquet(path)
        for i, r in sig.iterrows():
            d0 = pd.Timestamp(r["日期"])
            if r["候選"] not in closes.columns or d0 not in closes.index:
                continue
            s = closes[r["候選"]].dropna()
            after = s[s.index > d0]
            base = s.loc[d0]
            if len(after) >= 5 and pd.isna(r["5日後%"]):
                sig.at[i, "5日後%"] = round((after.iloc[4] / base - 1) * 100, 1)
            if len(after) >= 10 and pd.isna(r["10日後%"]):
                sig.at[i, "10日後%"] = round((after.iloc[9] / base - 1) * 100, 1)
                sig.at[i, "狀態"] = "已驗證"
            m = tg[(tg["發布時間"] > d0) & (tg["發布時間"] <= d0 + pd.Timedelta(days=10))
                   & tg["個股代號"].astype(str).str.contains(rf"(?:^|,){r['候選']}(?:,|$)")]
            sig.at[i, "之後出現同題材新聞"] = bool((m["themes"].str.contains(re.escape(r["題材"]), na=False)).any())
        sig.to_parquet(path, index=False)
        done = sig[sig["狀態"] == "已驗證"]
        if len(done):
            print(f"{path.name}: 已驗證 {len(done)} 筆，10 日平均 {done['10日後%'].mean():+.1f}%，"
                  f"正報酬 {(done['10日後%'] > 0).mean():.0%}，之後出現同題材新聞 {done['之後出現同題材新聞'].mean():.0%}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    sys.exit(validate() if a.validate else detect(not a.no_push, a.dry))
