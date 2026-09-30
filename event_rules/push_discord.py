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
SUMMARY_LEN = 110


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


def summaries(ids: set) -> dict:
    """id (新聞連結 / MOPS鍵) → 一句摘要。新聞取摘要或內文第一句；重訊取抽出的重點。"""
    out = {}
    for f in sorted(glob.glob("/mnt/d/mops/news/data/新聞_*.parquet"))[-2:]:
        d = pd.read_parquet(f, columns=["連結", "摘要", "內文"])
        d = d[d["連結"].isin(ids)]
        for link, summ, body in zip(d["連結"], d["摘要"], d["內文"]):
            text = (body or summ or "").replace("\n", " ").strip()
            out[link] = text[:SUMMARY_LEN] + ("…" if len(text) > SUMMARY_LEN else "")
    for f in sorted(glob.glob("/mnt/d/mops/material_info/derived/重訊事件_*.parquet"))[-2:]:
        d = pd.read_parquet(f, columns=["MOPS鍵", "數據"])
        d = d[d["MOPS鍵"].isin(ids) & (d["數據"] != "")]
        for key, data in zip(d["MOPS鍵"], d["數據"]):
            j = json.loads(data)
            if "澄清" in j:
                c = j["澄清"]
                out[key] = f"報導：{c.get('報導內容', '')[:60]}｜公司：{c.get('公司說明', '')[:80]}".replace("\n", "")
            elif "私募" in j:
                p = j["私募"]
                out[key] = (f"私募 {p.get('私募股數', 0):,.0f} 股，每股 {p.get('私募價格', '?')} 元"
                            f"（參考價 {p.get('參考價格', '?')}，折價 {p.get('折價率_pct', '?')}%）")
            elif "自結" in j:
                a = j["自結"]
                out[key] = (f"{a.get('資料月份', '')} 自結：營收 {a.get('營收_最近一月', '?')}、"
                            f"EPS {a.get('EPS_最近一月', '?')} 元（年增 {a.get('EPS_月年增_pct', '?')}%）")
    return out


def fmt(r, nm: dict, summ: dict) -> str:
    """乾淨格式：日期時間 代號 名稱 多/空 標題，第二行摘要，第三行連結。"""
    codes = [c for c in str(r.stocks).split(",") if c][:1]
    code = codes[0] if codes else "－"
    name = nm.get(code, "") if codes else ""
    line = f"{r.time:%Y-%m-%d %H:%M:%S} {code} {name} {r.direction or '－'} {r.title}".replace("  ", " ")
    lines = [line]
    if summ.get(r.id):
        lines.append(summ[r.id])
    if str(r.id).startswith("http"):
        lines.append(f"<{r.id}>")
    return "\n".join(lines)


def instant(dry: bool, resend_hours: int = 0) -> int:
    sc, nm = load(), names()
    pushed = set(json.loads(STATE.read_text()) if STATE.exists() else [])
    first_run = not STATE.exists()
    hours = resend_hours or LOOKBACK_HOURS
    recent = sc[(sc["time"] >= pd.Timestamp.now() - pd.Timedelta(hours=hours))
                & (sc["event_score"] >= THRESHOLD)]
    best = recent.sort_values("event_score", ascending=False).drop_duplicates("event_id")
    todo = (best if resend_hours else best[~best["event_id"].isin(pushed)]).sort_values("time")
    todo = todo.head(50 if resend_hours else MAX_PER_RUN)
    summ = summaries(set(todo["id"]))
    for r in todo.itertuples(index=False):
        msg = fmt(r, nm, summ)
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
    lines = [f"**{today:%Y-%m-%d} 當日總結（前 {len(g)} 名）**"]
    for r in g.itertuples(index=False):
        codes = [c for c in str(r.stocks).split(",") if c][:1]
        code = codes[0] if codes else "－"
        lines.append(f"{r.time:%Y-%m-%d %H:%M:%S} {code} {nm.get(code, '') if codes else ''} "
                     f"{r.direction or '－'} {r.title[:50]}".replace("  ", " "))
    msg = "\n".join(lines)
    print(msg)
    if not dry and len(g):
        notify_discord.post(msg, webhook_url=webhook(), code_block=False)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["instant", "daily"], default="instant")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--resend-hours", type=int, default=0, help="重發最近 N 小時內達門檻的事件 (不看是否推過)")
    a = ap.parse_args()
    sys.exit(instant(a.dry, a.resend_hours) if a.mode == "instant" else daily(a.dry))
