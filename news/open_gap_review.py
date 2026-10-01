#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
開盤跳空檢討 (永豐 Shioaji 快照)
================================================================
09:05 抓全市場快照 → 算每檔開盤跳空 (開盤價 / 昨收 − 1) → 扣掉大盤與類股後找異常 → 對照前一日
13:30 起到今早的新聞/重訊評分，分三類：
  命中          有新聞且系統事件分 ≥ 7
  有新聞系統低分 有新聞但事件分 < 7      ← 規則要檢討
  無新聞大漲跌   找不到相關新聞/重訊     ← 另存一檔，獨立研究 (籌碼/法人/其他因素)
異常門檻：相對跳空 = 個股跳空 − 類股跳空中位數；|相對跳空| ≥ max(GAP_MIN, GAP_SIGMA × 類股跳空標準差)
輸出：D:/mops/news/open_snapshots/開盤快照_YYYY-MM-DD.parquet (全市場原始快照)
      D:/mops/news/review/跳空檢討_YYYY-MM-DD.csv、無新聞大漲跌_YYYY-MM-DD.csv
      Discord 一則當日摘要 (經 guarded_send)
執行：daily_script/.venv-intraday/bin/python news/open_gap_review.py   (shioaji 只裝在那個 venv)
金鑰：SJ_API_KEY / SJ_SEC_KEY，依序找 data_collector/.env、daily_script/.env、daily_notify.old/disposal_intraday/.env
"""
import glob
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
DC_ROOT = HERE.parent
sys.path.insert(0, str(DC_ROOT))
SNAP_DIR = Path("/mnt/d/mops/news/open_snapshots")
REVIEW_DIR = Path("/mnt/d/mops/news/review")
SCORED = "/mnt/d/mops/news/scored/新鮮度_*.parquet"
INDUSTRY = "/mnt/d/mops/MopsIndustry/*/MopsIndustry_*.parquet"
STOCK_INFO = "/mnt/d/finmind_data/TaiwanStockInfo/TaiwanStockInfo.parquet"
ENV_FILES = [DC_ROOT / ".env", Path("/home/tearicee/daily_script/.env"),
             Path("/home/tearicee/daily_notify.old/disposal_intraday/.env")]
GAP_MIN, GAP_SIGMA, SCORE_HIT = 3.0, 2.0, 7.0


def load_keys() -> tuple:
    env = {}
    for f in ENV_FILES:
        if f.exists():
            for line in f.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env.setdefault(k.strip(), v.strip().strip('"'))
    key, sec = env.get("SJ_API_KEY") or env.get("SHIOAJI_API_KEY"), env.get("SJ_SEC_KEY") or env.get("SHIOAJI_SECRET_KEY")
    if not (key and sec):
        raise SystemExit("找不到 SJ_API_KEY / SJ_SEC_KEY")
    return key, sec


def take_snapshot(day: str) -> pd.DataFrame:
    import shioaji as sj
    key, sec = load_keys()
    api = sj.Shioaji(simulation=True)
    api.login(api_key=key, secret_key=sec)
    try:
        uni = [c for c in api.contracts.list(sj.SecurityType.Stock)
               if re.fullmatch(r"[1-9]\d{3}", c.code) and str(c.exchange).split(".")[-1] in ("TSE", "OTC")]
        rows = []
        for i in range(0, len(uni), 500):
            for s in api.snapshots(uni[i:i + 500]):
                d = dict(s.__dict__)
                d["ts"] = pd.Timestamp(d["ts"], unit="ns").tz_localize("UTC").tz_convert("Asia/Taipei").tz_localize(None)
                for k in ("exchange", "tick_type", "change_type"):   # enum → 字串
                    d[k] = str(d[k]).split(".")[-1]
                for k, v in list(d.items()):
                    if not isinstance(v, (int, float, str, pd.Timestamp)) and v is not None:
                        d[k] = str(v)
                rows.append(d)
        ref = {}
        for c in uni:  # 昨收 (contracts.info 的 reference)
            try:
                ref[c.code] = float(api.contracts.info(c).reference)
            except Exception:  # noqa: BLE001
                pass
    finally:
        api.logout()
    df = pd.DataFrame(rows)
    df["reference"] = df["code"].map(ref)
    df["snapshot_day"] = day
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(SNAP_DIR / f"開盤快照_{day}.parquet", compression="zstd", index=False)
    return df


def analyze(df: pd.DataFrame, day: str) -> tuple:
    df = df[(df["open"] > 0) & (df["reference"] > 0)].copy()
    df["跳空%"] = ((df["open"] / df["reference"] - 1) * 100).round(2)
    f = sorted(glob.glob(INDUSTRY))
    ind = pd.read_parquet(f[-1]) if f else pd.DataFrame(columns=["stock_id", "category"])
    info = pd.read_parquet(STOCK_INFO).sort_values("date").drop_duplicates("stock_id", keep="last")
    df["產業"] = df["code"].map(dict(zip(ind["stock_id"].astype(str), ind["category"]))).fillna("")
    df["名稱"] = df["code"].map(dict(zip(info["stock_id"], info["stock_name"]))).fillna("")
    market_gap = df["跳空%"].median()
    g = df.groupby("產業")["跳空%"]
    df["類股跳空%"] = g.transform("median").round(2)
    df["類股標準差"] = g.transform("std").fillna(df["跳空%"].std()).round(2)
    df["相對跳空%"] = (df["跳空%"] - df["類股跳空%"]).round(2)
    thr = (GAP_SIGMA * df["類股標準差"]).clip(lower=GAP_MIN)
    flagged = df[df["相對跳空%"].abs() >= thr].copy()

    # 前一日 13:30 → 今早 的事件
    sc = pd.concat(pd.read_parquet(x) for x in sorted(glob.glob(SCORED))[-2:])
    prev_close = None
    days = sorted(set(sc["time"].dt.normalize()))
    today = pd.Timestamp(day)
    prev_trade = max((d for d in days if d < today), default=today - pd.Timedelta(days=1))
    win = sc[(sc["time"] >= prev_trade + pd.Timedelta(hours=13, minutes=30)) & (sc["time"] < today + pd.Timedelta(hours=9))]
    rows, nonews = [], []
    for r in flagged.reindex(flagged["相對跳空%"].abs().sort_values(ascending=False).index).to_dict("records"):
        ev = win[win["stocks"].astype(str).str.contains(rf"(?:^|,){r['code']}(?:,|$)")]
        base = {"日期": day, "代號": r["code"], "名稱": r["名稱"], "產業": r["產業"], "跳空%": r["跳空%"],
                "類股跳空%": r["類股跳空%"], "相對跳空%": r["相對跳空%"], "類股標準差": r["類股標準差"],
                "大盤跳空%": round(market_gap, 2), "開盤價": r["open"], "昨收": r["reference"], "量比": r["volume_ratio"]}
        if ev.empty:
            nonews.append(base)
            continue
        top = ev.sort_values("event_score", ascending=False).iloc[0]
        rows.append({**base, "類別": "命中" if top["event_score"] >= SCORE_HIT else "有新聞系統低分",
                     "最高事件分": top["event_score"], "新聞數": len(ev), "標題": top["title"], "方向": top["direction"],
                     "評分理由": top["reasons"], "連結或MOPS鍵": top["id"], "我的評價": ""})
    return pd.DataFrame(rows), pd.DataFrame(nonews), round(market_gap, 2)


def main() -> int:
    day = datetime.now().strftime("%Y-%m-%d")
    path = SNAP_DIR / f"開盤快照_{day}.parquet"
    df = pd.read_parquet(path) if path.exists() and "--reuse" in sys.argv else take_snapshot(day)
    if (df["total_volume"] > 0).sum() < 100:
        print(f"{day} 幾乎沒有成交量，非交易日或尚未開盤，略過")
        return 0
    rev, nonews, mkt = analyze(df, day)
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    rev.to_csv(REVIEW_DIR / f"跳空檢討_{day}.csv", index=False, encoding="utf-8-sig")
    nonews.to_csv(REVIEW_DIR / f"無新聞大漲跌_{day}.csv", index=False, encoding="utf-8-sig")
    n_hit = int((rev.get("類別") == "命中").sum()) if len(rev) else 0
    n_low = len(rev) - n_hit
    lines = [f"**{day} 開盤跳空檢討**（大盤跳空 {mkt:+.2f}%；異常 {len(rev) + len(nonews)} 檔：命中 {n_hit}、有新聞系統低分 {n_low}、無新聞 {len(nonews)}）"]
    for r in rev.head(8).to_dict("records"):
        lines.append(f"{r['類別']}｜{r['代號']} {r['名稱']} {r['相對跳空%']:+.1f}%｜<{r['最高事件分']}> {str(r['標題'])[:40]}")
    for r in nonews.head(5).to_dict("records"):
        lines.append(f"無新聞｜{r['代號']} {r['名稱']} {r['相對跳空%']:+.1f}%（{r['產業']}）")
    msg = "\n".join(lines)
    print(msg)
    if "--no-push" not in sys.argv:
        from event_rules import push_discord as pdc
        pdc.guarded_send([{"key": f"gap-{day}", "time": pd.Timestamp.now(), "score": 10.0, "text": msg}])
    try:
        from common import heartbeat
        heartbeat.write("open_gap_review", heartbeat.STATUS_OK, 0, f"異常 {len(rev) + len(nonews)}",
                        stats={"hit": n_hit, "low": n_low, "nonews": len(nonews)})
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
