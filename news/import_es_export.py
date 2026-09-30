#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
匯入舊 Elasticsearch 匯出檔 (新聞資料_逐筆版.jsonl，daily_script 舊爬蟲的 news_* 索引)
================================================================
每行 {"_index":"news_YYYYMMDD[_kind]","_id":..,"_source":{title,content,publish_time,source,url|content_link,…}}
  kind        內容                                   處理
  (無)/media  工商時報、經濟日報網頁 (多為標題+摘要)   → 新聞庫，來源依網址判斷
  anue        鉅亨網 (全文)                           → 新聞庫 (多數已由回補取得，重複連結略過)
  udn         經濟日報 RSS (摘要；時間比台灣時間晚 8h)  → 新聞庫，發布時間 -8h
  technews    科技新報 RSS (摘要；時間早 8h)            → 新聞庫，發布時間 +8h
  twse_info   重大訊息 (只有主旨，無說明)              → D:/mops/material_info/history/重訊主旨歷史_YYYY-MM.parquet
規則：
  - 以連結去重 (去掉 ?query)；同連結多筆取內文最長者。庫內已有的連結不覆寫 (現行爬蟲的內文較完整)。
  - 分類欄標「歷史匯入」；只有摘要的文章之後可用 download_news.py --mode fulltext 補全文。
  - 時間校正值來自與現有資料同連結比對 (udn +8、technews -8) 及匯出手冊說明。
用法：python import_es_export.py /mnt/d/mops/news/import/新聞資料_逐筆版.jsonl
"""
import json
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store  # noqa: E402

HOUR_FIX = {"udn": -8, "technews": 8}
SRC = {"anue": "鉅亨網", "udn": "經濟日報", "technews": "科技新報"}
MOPS_DIR = Path("/mnt/d/mops/material_info/history")


def main(path: str) -> int:
    news, mops = {}, []
    now = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            s = r["_source"]
            kind = re.sub(r"news_\d{8}_?", "", r["_index"]) or "media"
            if kind == "twse_info":
                mops.append({"公司代號": str(s.get("stock_id") or ""), "公司簡稱": s.get("stock_name") or "",
                             "發布時間": s.get("publish_time"), "主旨": (s.get("title") or "").strip(),
                             "來源": "ES匯出(主旨)"})
                continue
            url = (s.get("url") or s.get("content_link") or "").split("?")[0].strip()
            t = pd.to_datetime(s.get("publish_time"), errors="coerce", format="mixed")
            if not url or pd.isna(t):
                continue
            t = t + pd.Timedelta(hours=HOUR_FIX.get(kind, 0))
            body = (s.get("content") or "").replace("\r\n", "\n").strip()
            src = SRC.get(kind) or ("工商時報" if "ctee.com.tw" in url else "經濟日報" if "udn.com" in url else "其他")
            old = news.get(url)
            if old is None or len(body) > len(old["內文"]):
                news[url] = {"發布時間": t.strftime("%Y-%m-%d %H:%M:%S"), "來源": src, "分類": "歷史匯入",
                             "標題": (s.get("title") or "").strip(), "摘要": body[:300], "內文": body,
                             "連結": url, "個股代號": "", "關鍵字": "", "抓取時間": now}
    rows = list(news.values())
    print(f"新聞：不重複連結 {len(rows):,}")
    added = 0
    for i in range(0, len(rows), 50000):   # 分批寫，避免單次佔用太多記憶體
        added += store.upsert(rows[i:i + 50000])
    print(f"  新增 {added:,} (其餘連結庫內已有，未覆寫)")

    m = pd.DataFrame(mops)
    m["發布時間"] = pd.to_datetime(m["發布時間"], errors="coerce", format="mixed")
    m = m.dropna(subset=["發布時間"]).drop_duplicates(["公司代號", "發布時間", "主旨"]).sort_values("發布時間")
    MOPS_DIR.mkdir(parents=True, exist_ok=True)
    for ym, g in m.groupby(m["發布時間"].dt.strftime("%Y-%m")):
        g.to_parquet(MOPS_DIR / f"重訊主旨歷史_{ym}.parquet", compression="zstd", index=False)
    print(f"重訊主旨：{len(m):,} 筆 ({m['發布時間'].min()} ~ {m['發布時間'].max()}) → {MOPS_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
