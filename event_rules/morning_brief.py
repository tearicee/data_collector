#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盤前晨報 (08:20)：使用者 2026-11-09「每天早上 8 點 20 準時從本地端整理好新聞用排程發送」
================================================================
一則訊息三段，全部來自本機已算好的資料 (不上網、不另外抓)：
  1. 昨 14:00 起達門檻 (事件分 ≥7) 的事件，含即時已推過的 (標「已推」)，補漏的標「補」
  2. 題材熱點 (theme_heat 盤前視窗)
  3. 今日事件 (event_calendar：今天的法說/股東會/上市櫃買賣日 + 新聞裡帶今天日期的事件句；高頻數據不列)
由 push_discord --mode daily 在上午呼叫 (既有 08:20 cron)，晚間 21:30 仍是補漏。
用法：python -m event_rules.morning_brief --dry
"""
import argparse
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TOP_EVENTS, TOP_TODAY = 20, 10


def events_section(now: pd.Timestamp) -> list:
    from event_rules import push_discord as P
    sc, nm = P.load(), P.names()
    start = (now.normalize() - pd.Timedelta(days=1)) + pd.Timedelta(hours=14)
    g = sc[(sc["time"] >= start) & (sc["event_score"] >= P.THRESHOLD)]
    g = g.sort_values(["score", "event_score"], ascending=False).drop_duplicates("event_id").head(TOP_EVENTS)
    pushed, mem = set(P._state()["pushed"]), P.members(sc)
    lines = [f"**1. 昨 14:00 起達門檻事件 {len(g)} 則**"]
    for r in g.itertuples(index=False):
        seen = P._seen({"key": r.event_id, "members": mem.get(r.event_id, [])}, pushed)
        lines.append(("　" if seen else "補 ") + P.fmt(r, nm).replace(f"<{r.event_score:.1f}>", f"<{r.score:.1f}>"))
    return lines


def theme_section(now: pd.Timestamp) -> list:
    from event_rules import theme_heat as T
    try:
        msg, rows, _ = T.compose("pre", now)
        T.save_rows(rows, now.normalize(), "pre")
    except Exception as e:  # noqa: BLE001  (價格檔缺、假日等，不要讓晨報整個失敗)
        return [f"**2. 題材熱點**：無法計算 ({type(e).__name__}: {str(e)[:60]})"]
    body = msg.split("\n", 1)[1] if "\n" in msg else ""
    return [f"**2. 題材熱點 {len(rows)} 個**"] + ([body] if body else [])


def today_section(now: pd.Timestamp) -> list:
    from event_rules import event_calendar as C
    ne, me, _, lo, _ = C.build(now, now.normalize(), days=1)
    lines = [f"**3. 今日事件 {lo:%m/%d} 週{C.WD[lo.weekday()]}**"]
    if len(me):
        for kind, g in me.groupby("類型"):
            hot = g[g["熱度"] >= 3].head(8)
            desc = "、".join(f"{x.名稱}{'(' + x.目的 + ')' if x.目的 else ''}" for x in hot.itertuples(index=False))
            rest = "、".join(f"{x.名稱}" for x in g[g["熱度"] < 3].head(6).itertuples(index=False))
            lines.append(f"· {kind} {len(g)} 家" + (f"｜熱門：{desc}" if desc else "") + (f"｜{rest}{'…' if len(g) > len(hot) + 6 else ''}" if rest else ""))
    if len(ne):
        for r in ne[ne["類型"] != "高頻數據"].head(TOP_TODAY).itertuples(index=False):
            s = re.sub(r"^(周|週)[一二三四五六日]\s*[（(][^)）]*[)）]\s*", "", r.事件)
            lines.append(f"· {s[:90]}" + ("" if r.彙整文 else f"（{r.來源}）"))
    if len(lines) == 1:
        lines.append("· 無")
    return lines


def build(now: pd.Timestamp | None = None) -> str:
    now = now or pd.Timestamp.now()
    parts = [f"**{now:%Y-%m-%d %H:%M} 盤前晨報**"]
    for fn in (events_section, theme_section, today_section):
        try:
            parts.append("\n".join(fn(now)))
        except Exception as e:  # noqa: BLE001
            parts.append(f"**{fn.__name__}**：失敗 ({type(e).__name__}: {str(e)[:80]})")
    return "\n\n".join(parts)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--asof", help="假裝在這個時間 (測試用)")
    a = ap.parse_args()
    now = pd.Timestamp(a.asof) if a.asof else pd.Timestamp.now()
    msg = build(now)
    print(msg)
    if not a.dry:
        from common import notify_discord
        from event_rules.push_discord import webhook
        notify_discord.post(msg, webhook_url=webhook(), code_block=False)
