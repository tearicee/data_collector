#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新鮮度推播 → Discord「新聞新鮮度」頻道 (webhook：daily_script/.env 的 DISCORD_WEBHOOK_URL_NEWS_FRESHNESS)
  --mode instant   事件分 ≥ 門檻、發布於最近 LOOKBACK_HOURS 小時內、尚未推過的事件，一則一個訊息
                   (同一事件的多篇報導只推一次；已推過的記在 state/pushed_events.json)
  --mode daily     當日前 N 名總結 (含市場反應分)
  --dry            只印不送
"""
import argparse
import glob
import json
import os
import sys
from pathlib import Path

import pandas as pd
from dotenv import dotenv_values

DC_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DC_ROOT))
from common import notify_discord  # noqa: E402

SCORED = "/mnt/d/mops/news/scored/新鮮度_*.parquet"
STATE = Path("/mnt/d/mops/news/state/pushed_events.json")
STOCK_INFO = "/mnt/d/finmind_data/TaiwanStockInfo/TaiwanStockInfo.parquet"
ENV_VAR = "DISCORD_WEBHOOK_URL_NEWS_FRESHNESS"
THRESHOLD, LOOKBACK_HOURS, MAX_PER_RUN, DAILY_TOP = 7.0, 6, 10, 15


def webhook() -> str:
    url = os.environ.get(ENV_VAR) or dotenv_values(DC_ROOT / ".env").get(ENV_VAR) \
        or dotenv_values("/home/tearicee/daily_script/.env").get(ENV_VAR)
    if not url:
        raise RuntimeError(f"找不到 {ENV_VAR}")
    return url


def load() -> pd.DataFrame:
    files = sorted(glob.glob(SCORED))[-2:]
    return pd.concat(pd.read_parquet(f) for f in files)


def names() -> dict:
    try:
        d = pd.read_parquet(STOCK_INFO, columns=["stock_id", "stock_name"]).drop_duplicates("stock_id", keep="last")
        return dict(zip(d["stock_id"], d["stock_name"]))
    except Exception:  # noqa: BLE001
        return {}


def stock_label(stocks: str, nm: dict) -> str:
    codes = [c for c in str(stocks).split(",") if c][:3]
    return "、".join(f"{c} {nm.get(c, '')}".strip() for c in codes)


def fmt(r, nm: dict) -> str:
    icon = "🔴" if r.score >= 9 else ("🟠" if r.score >= 8 else "🟡")
    head = f"{icon} **{r.event_score:.1f} 分**" + (f"｜{r.direction}" if r.direction else "")
    who = stock_label(r.stocks, nm)
    head += f"｜{who}" if who else ""
    lines = [head, f"**{r.title}**"]
    lines += [f"• {x}" for x in str(r.reasons).split("；") if x]
    if r.market_reason:
        lines += [f"• 市場：{x}" for x in str(r.market_reason).split("；") if x]
    src = f"{r.source} {r.time:%m-%d %H:%M}"
    if r.n_reports > 1:
        src += f"｜共 {r.n_reports} 篇 / {r.n_sources} 家"
    link = r.id if str(r.id).startswith("http") else ""
    lines.append(f"{src}" + (f"\n<{link}>" if link else ""))
    return "\n".join(lines)


def instant(dry: bool) -> int:
    sc, nm = load(), names()
    pushed = set(json.loads(STATE.read_text()) if STATE.exists() else [])
    first_run = not STATE.exists()
    recent = sc[(sc["time"] >= pd.Timestamp.now() - pd.Timedelta(hours=LOOKBACK_HOURS))
                & (sc["event_score"] >= THRESHOLD)]
    best = recent.sort_values("event_score", ascending=False).drop_duplicates("event_id")
    todo = best[~best["event_id"].isin(pushed)].sort_values("time").head(MAX_PER_RUN)
    for r in todo.itertuples(index=False):
        msg = fmt(r, nm)
        print(msg, "\n")
        if not dry:
            notify_discord.post(msg, webhook_url=webhook(), code_block=False)
            pushed.add(r.event_id)
    if not dry and (len(todo) or first_run):
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(sorted(pushed)[-5000:]))
    print(f"instant: 候選 {len(best)}，推送 {len(todo)}")
    return 0


def daily(dry: bool) -> int:
    sc, nm = load(), names()
    today = pd.Timestamp.now().normalize()
    g = sc[sc["time"] >= today - pd.Timedelta(hours=10)]  # 前一晚 14:00 後 ~ 現在
    g = g.sort_values(["score", "event_score"], ascending=False).drop_duplicates("event_id").head(DAILY_TOP)
    lines = [f"📋 **{today:%m/%d} 新鮮度前 {len(g)} 名** (總分 = 事件分 + 市場分)"]
    for r in g.itertuples(index=False):
        m = "" if r.market_score != r.market_score else f"+{r.market_score:.0f}"
        lines.append(f"`{r.score:>4.1f}` ({r.event_score:.1f}{m}) {r.direction or '－'} "
                     f"{stock_label(r.stocks, nm) or r.source}｜{r.title[:48]}")
    msg = "\n".join(lines)
    print(msg)
    if not dry and len(g):
        notify_discord.post(msg, webhook_url=webhook(), code_block=False)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["instant", "daily"], default="instant")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    sys.exit(instant(a.dry) if a.mode == "instant" else daily(a.dry))
