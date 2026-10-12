#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
法說會逐字稿與重點摘要 (AlphaMemo 免費頁) → /mnt/d/mops/news/earnings_call/法說摘要_YYYY-MM.parquet (YYYY-MM = 法說日)
================================================================
  來源：https://www.alphamemo.ai/free-transcripts (公開法說會錄音的 AI 逐字稿與 Memo)。
  頁面是 Next.js，資料在 self.__next_f 的 flight 字串裡：
    清單頁  initialRows: [{transcriptId, earningsCallId, stockNumber, stockName, date, durationSec, deckZh(法說簡報 PDF, MOPS), ...}]
            (2026-10-12 改版前叫 initialTranscripts/stock_number/audio_date/audio_length_ceil_sec/deck_zh；transcriptId 為 null 的是尚未舉行的場次)
    內容頁  {metadata, memo:[{zh_heading, items:[{text,...}], hidden_item_count}], processed_transcript:[{segments:[{start_sec,text,speaker}]}],
             excerpt:{end_sec, transcript_truncated}}
  免費頁只開放：亮點全部、其他段落各第 1 點 (hidden_item_count 是沒顯示的)、逐字稿前約 11 分鐘。
  使用者 2026-10-07 要求抓取 (投資人有權取得的公開法說內容、自用研究、該站無 API)；該站服務條款禁止未經許可的自動化存取，
  風險由使用者承擔。因此只做最低限度：每天 1 次清單 + 只抓沒抓過的內容頁，每頁間隔 8~12 秒、每天最多 MAX_PAGES 頁，
  只存本機 D 槽、不轉發、不重製到任何對外服務。
用法：python news/collect_earnings_calls.py [--max 25] [--dry]
"""
import argparse
import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import download_news as dn  # noqa: E402
from common import heartbeat  # noqa: E402

BASE = "https://www.alphamemo.ai"
LIST_URL = BASE + "/free-transcripts"
OUT_DIR = Path("/mnt/d/mops/news/earnings_call")
MAX_PAGES = 25
SLEEP = (8, 12)
FIELDS = ["公司代號", "公司名稱", "法說日期", "時長_分", "uuid", "連結", "簡報PDF", "亮點", "摘要", "逐字稿", "逐字稿截斷", "抓取時間"]


def flight(html: str) -> str:
    """把 self.__next_f.push([1,"..."]) 的字串接起來。"""
    return "".join(json.loads(m[1]) for m in re.finditer(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', html))


def list_calls() -> list:
    """回傳統一欄位 [{id, stock_number, stock_name, audio_date, audio_length_ceil_sec, deck_zh}]，兩種版本的清單都認得。"""
    f = flight(dn.get(LIST_URL).text)
    for key in ('"initialRows":', '"initialTranscripts":'):
        i = f.find(key)
        if i >= 0:
            break
    else:
        raise RuntimeError("清單頁找不到 initialRows/initialTranscripts (頁面結構變了?)")
    arr, _ = json.JSONDecoder().raw_decode(f[i + len(key):])
    out = []
    for x in arr:
        uid = x.get("transcriptId") if "transcriptId" in x else x.get("id")
        code = str(x.get("stockNumber") or x.get("stock_number") or "")
        if not uid or not re.fullmatch(r"\d{4,6}", code) or x.get("market", "TW") not in ("TW", "上市", "上櫃", "興櫃"):
            continue
        out.append({"id": uid, "stock_number": code, "stock_name": x.get("stockName") or x.get("stock_name") or "",
                    "audio_date": x.get("date") or x.get("audio_date") or "",
                    "audio_length_ceil_sec": x.get("durationSec") or x.get("audio_length_ceil_sec") or 0,
                    "deck_zh": x.get("deckZh") or x.get("deck_zh") or ""})
    return out


def fetch_call(uid: str) -> dict:
    f = flight(dn.get(f"{BASE}/free-transcripts/{uid}").text)
    i = f.find('{"metadata":')
    if i < 0:
        raise RuntimeError("內容頁找不到 metadata")
    obj, _ = json.JSONDecoder().raw_decode(f[i:])
    memo = {}
    for sec in obj.get("memo") or []:
        texts = [it.get("text", "") for it in sec.get("items") or [] if it.get("text")]
        if texts or sec.get("hidden_item_count"):
            memo[sec.get("zh_heading") or sec.get("heading")] = {"items": texts, "hidden": int(sec.get("hidden_item_count") or 0)}
    segs = []
    for t in obj.get("processed_transcript") or []:
        segs += t.get("segments") or []
    transcript = "\n".join(f"[{int(s.get('start_sec', 0)) // 60:02d}:{int(s.get('start_sec', 0)) % 60:02d}] {s.get('text', '')}" for s in segs)
    return {"亮點": json.dumps(memo.get("亮點", {}).get("items", []), ensure_ascii=False),
            "摘要": json.dumps(memo, ensure_ascii=False), "逐字稿": transcript,
            "逐字稿截斷": bool((obj.get("excerpt") or {}).get("transcript_truncated", True))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=MAX_PAGES)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    have = set()
    for p in OUT_DIR.glob("法說摘要_*.parquet"):
        have |= set(pd.read_parquet(p, columns=["uuid"])["uuid"])
    try:
        calls = list_calls()
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] 清單: {e}")
        heartbeat.write("earnings_call", heartbeat.STATUS_FAIL, 1, str(e)[:200])
        return 1
    todo = [c for c in calls if c["id"] not in have][: a.max]
    print(f"{datetime.now():%F %T} 清單 {len(calls)} 場，未抓 {len([c for c in calls if c['id'] not in have])}，本次抓 {len(todo)}")
    rows, errors = [], 0
    for c in todo:
        time.sleep(random.uniform(*SLEEP))
        try:
            d = fetch_call(c["id"])
        except Exception as e:  # noqa: BLE001
            errors += 1
            print(f"  [WARN] {c['stock_number']} {c['stock_name']} {c['audio_date']}: {e}")
            continue
        rows.append({"公司代號": str(c["stock_number"]), "公司名稱": c.get("stock_name", ""), "法說日期": c["audio_date"],
                     "時長_分": round((c.get("audio_length_ceil_sec") or 0) / 60), "uuid": c["id"],
                     "連結": f"{BASE}/free-transcripts/{c['id']}", "簡報PDF": c.get("deck_zh") or "",
                     **d, "抓取時間": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        print(f"  {c['audio_date']} {c['stock_number']} {c['stock_name']} 亮點 {len(json.loads(d['亮點']))} 點，逐字稿 {len(d['逐字稿'])} 字{'(截斷)' if d['逐字稿截斷'] else ''}")
    if rows and not a.dry:
        df = pd.DataFrame(rows).reindex(columns=FIELDS)
        for ym, g in df.groupby(df["法說日期"].str[:7]):
            path = OUT_DIR / f"法說摘要_{ym}.parquet"
            old = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=FIELDS)
            out = pd.concat([old, g[~g["uuid"].isin(set(old["uuid"]))]], ignore_index=True)
            tmp = path.with_suffix(".parquet.tmp")
            out.to_parquet(tmp, compression="zstd", index=False)
            tmp.replace(path)
    ok = errors < max(1, len(todo))
    heartbeat.write("earnings_call", heartbeat.STATUS_OK if ok else heartbeat.STATUS_FAIL, 0 if ok else 1,
                    f"新增 {len(rows)}，失敗 {errors}", stats={"new": len(rows), "failed": errors})
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
