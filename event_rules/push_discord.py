#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新鮮度推播 → Discord「新聞新鮮度」頻道 (webhook：daily_script/.env 的 DISCORD_WEBHOOK_URL_NEWS_FRESHNESS)
  --mode instant   事件分 ≥ 門檻、發布於最近 LOOKBACK_HOURS 小時內、尚未推過的事件，一則一個訊息
                   (同一事件的多篇報導只推一次；已推過的記在 state/pushed_events.json)
  --mode daily     當日前 N 名總結 (含市場反應分)
  --dry            只印不送
  --test           測試模式：才允許 --resend-hours 重發舊訊息 (訊息前加 [測試])

防呆 (guarded_send，所有推播都經過這裡；非測試模式一律套用)：
  1. 只送「新的」：發布時間在 MAX_AGE_HOURS 小時內，且 key 沒推過 (state/pushed_events.json)
  2. 只送「高分」：分數 ≥ THRESHOLD
  3. 限量：單次最多 MAX_PER_RUN 則、每小時最多 MAX_PER_HOUR 則；超過的直接捨棄並記為已處理，
     不會留到下一輪補發 (避免來源一次吐出大量舊資料時洗版)
"""
import argparse
import glob
import json
import os
import sys
from pathlib import Path

import pandas as pd


def dotenv_values(path) -> dict:   # 不依賴 python-dotenv (永豐快照用的 venv 沒裝)
    out = {}
    try:
        for line in Path(path).read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out

DC_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DC_ROOT))
from common import notify_discord  # noqa: E402

SCORED = "/mnt/d/mops/news/scored/新鮮度_*.parquet"
STATE = Path("/mnt/d/mops/news/state/pushed_events.json")
STOCK_INFO = "/mnt/d/finmind_data/TaiwanStockInfo/TaiwanStockInfo.parquet"
ENV_VAR = "DISCORD_WEBHOOK_URL_NEWS_FRESHNESS"
THRESHOLD, LOOKBACK_HOURS, MAX_PER_RUN, DAILY_TOP = 7.0, 6, 10, 15
MAX_AGE_HOURS, MAX_PER_HOUR = 6, 20
SUMMARY_LEN = 110


def webhook() -> str:
    url = os.environ.get(ENV_VAR) or dotenv_values(DC_ROOT / ".env").get(ENV_VAR) \
        or dotenv_values("/home/tearicee/daily_script/.env").get(ENV_VAR)
    if not url:
        raise RuntimeError(f"找不到 {ENV_VAR}")
    return url


def _state() -> dict:
    if not STATE.exists():
        return {"pushed": [], "sent_times": []}
    d = json.loads(STATE.read_text())
    return {"pushed": d, "sent_times": []} if isinstance(d, list) else d   # 舊格式是純清單


def guarded_send(items: list, test: bool = False, dry: bool = False) -> int:
    """items: [{"key", "time"(Timestamp), "score"(float), "text"}]。回傳實際送出則數。"""
    from common import notify_discord as nd
    st = _state()
    pushed = set(st["pushed"])
    now = pd.Timestamp.now()
    sent_times = [t for t in st["sent_times"] if pd.Timestamp(t) >= now - pd.Timedelta(hours=1)]
    items = sorted(items, key=lambda x: x["time"])
    if test:
        ok, skipped = items[:MAX_PER_RUN], []
    else:
        fresh = [it for it in items if it["key"] not in pushed]
        ok = [it for it in fresh if it["time"] >= now - pd.Timedelta(hours=MAX_AGE_HOURS) and it["score"] >= THRESHOLD]
        room = max(0, min(MAX_PER_RUN, MAX_PER_HOUR - len(sent_times)))
        ok = sorted(ok, key=lambda x: -x["score"])[:room]
        ok = sorted(ok, key=lambda x: x["time"])
        skipped = [it for it in fresh if it not in ok]
    sent = 0
    for it in ok:
        text = ("[測試] " if test else "") + it["text"]
        print(text, "\n")
        if not dry:
            nd.post(text, webhook_url=webhook(), code_block=False)
            sent += 1
            sent_times.append(str(now))
    if not dry and not test:
        pushed |= {it["key"] for it in ok} | {it["key"] for it in skipped}   # 被擋下的也記錄，之後不補發
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"pushed": sorted(pushed)[-20000:], "sent_times": sent_times}))
    if skipped:
        print(f"防呆擋下 {len(skipped)} 則 (過舊 / 分數不足 / 超過限量)")
    return sent


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


def fmt(r, nm: dict, summ: dict | None = None) -> str:
    """單行格式：日期時間 <分數> 代號 名稱 多/空 <標題> <連結>"""
    codes = [c for c in str(r.stocks).split(",") if c][:1]
    code = codes[0] if codes else "－"
    name = nm.get(code, "") if codes else "－"
    link = f" <{r.id}>" if str(r.id).startswith("http") else ""
    heat = f"|熱{r.heat:.0f}" if getattr(r, "heat", 0) and r.heat >= 3 else ""
    return f"{r.time:%Y-%m-%d %H:%M:%S} <{r.event_score:.1f}{heat}> {code} {name} {r.direction or '－'} <{r.title}>{link}"


def instant(dry: bool, resend_hours: int = 0, test: bool = False) -> int:
    sc, nm = load(), names()
    hours = resend_hours if test and resend_hours else LOOKBACK_HOURS
    recent = sc[sc["time"] >= pd.Timestamp.now() - pd.Timedelta(hours=hours)]
    best = recent.sort_values("event_score", ascending=False).drop_duplicates("event_id")
    if test:
        best = best[best["event_score"] >= THRESHOLD]
    items = [{"key": r.event_id, "time": r.time, "score": float(r.event_score), "text": fmt(r, nm)}
             for r in best.itertuples(index=False)]
    n = guarded_send(items, test=test, dry=dry)
    print(f"instant: 候選 {len(items)}，推送 {n}")
    return 0


def daily(dry: bool) -> int:
    sc, nm = load(), names()
    now = pd.Timestamp.now()
    today = now.normalize()
    # 08:20 盤前版：昨天 14:00 以後到現在；21:30 晚間版：今天 14:00 以後 (收盤後的重訊/新聞)
    start = (today - pd.Timedelta(days=1) if now.hour < 12 else today) + pd.Timedelta(hours=14)
    g = sc[sc["time"] >= start]
    g = g.sort_values(["score", "event_score"], ascending=False).drop_duplicates("event_id").head(DAILY_TOP)
    label = "盤前總結（昨 14:00 起）" if now.hour < 12 else "晚間總結（今 14:00 起）"
    lines = [f"**{now:%Y-%m-%d %H:%M} {label}，前 {len(g)} 名**"]
    for r in g.itertuples(index=False):
        lines.append(fmt(r, nm).replace(f"<{r.event_score:.1f}>", f"<{r.score:.1f}>"))
    msg = "\n".join(lines)
    print(msg)
    if not dry and len(g):
        notify_discord.post(msg, webhook_url=webhook(), code_block=False)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["instant", "daily"], default="instant")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--test", action="store_true", help="測試模式 (訊息加 [測試]，可搭配 --resend-hours)")
    ap.add_argument("--resend-hours", type=int, default=0, help="僅測試模式有效：重發最近 N 小時內達門檻的事件")
    a = ap.parse_args()
    if a.resend_hours and not a.test:
        sys.exit("--resend-hours 只能搭配 --test 使用 (防呆：非測試不重發舊訊息)")
    sys.exit(instant(a.dry, a.resend_hours, a.test) if a.mode == "instant" else daily(a.dry))
