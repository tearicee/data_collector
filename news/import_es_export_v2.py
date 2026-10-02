#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
匯入 2026-10-01 版 ES 匯出 (已補全文、時間已校正為台灣時間、重訊含說明)
  新聞：依連結比對新聞庫
    - 庫內沒有 → 新增 (分類=歷史匯入)
    - 庫內有且這份內文較長 → 以這份內文取代；若該列是舊版匯入 (分類=歷史匯入) 一併改用這份的發布時間
      (舊版匯入時對 RSS 來源做過 ±8 小時推估，這版已是正確時間)
  重訊：只匯入「我們自己開始收集之前」(< OWN_START) 的公告進重訊主庫，來源=es_export
        (之後的日期主庫已有含 MOPS鍵 的完整資料，不重複匯入)
用法：python import_es_export_v2.py /mnt/d/mops/news/import2/新聞資料_逐筆版.jsonl
"""
import json
import re
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import store  # noqa: E402

SRC = {"anue": "鉅亨網", "udn": "經濟日報", "technews": "科技新報"}
OWN_START = "2026-08-28"


def main(path: str) -> int:
    news, mops = {}, []
    now = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            s = r["_source"]
            kind = re.sub(r"news_\d{8}_?", "", r["_index"]) or "media"
            if kind == "twse_info":
                mops.append((str(s.get("stock_id") or ""), s.get("stock_name") or "", s.get("publish_time"),
                             (s.get("title") or "").strip(), (s.get("content") or "").replace("\r\n", "\n").strip()))
                continue
            url = (s.get("url") or s.get("content_link") or "").split("?")[0].strip()
            t = pd.to_datetime(s.get("publish_time"), errors="coerce", format="mixed")
            if not url or pd.isna(t):
                continue
            body = (s.get("content") or "").replace("\r\n", "\n").strip()
            src = SRC.get(kind) or ("工商時報" if "ctee.com.tw" in url else "經濟日報" if "udn.com" in url else "其他")
            old = news.get(url)
            if old is None or len(body) > len(old["內文"]):
                news[url] = {"發布時間": t.strftime("%Y-%m-%d %H:%M:%S"), "來源": src, "分類": "歷史匯入",
                             "標題": (s.get("title") or "").strip(), "摘要": body[:300], "內文": body,
                             "連結": url, "個股代號": "", "關鍵字": "", "抓取時間": now}
    df = pd.DataFrame(news.values())
    df["發布時間"] = pd.to_datetime(df["發布時間"])
    print(f"新聞：不重複連結 {len(df):,}")

    # --- 先更新庫內已有的連結 (逐月)
    updated = retimed = 0
    by_link = df.set_index("連結")
    with store.write_lock():
        for path_m in sorted(store.DATA_DIR.glob("新聞_*.parquet")):
            old = pd.read_parquet(path_m)
            hit = old["連結"].isin(by_link.index)
            if not hit.any():
                continue
            new_body = old.loc[hit, "連結"].map(by_link["內文"])
            longer = new_body.str.len() > old.loc[hit, "內文"].fillna("").str.len()
            idx = longer[longer].index
            if len(idx) == 0:
                continue
            old.loc[idx, "內文"] = new_body.loc[idx]
            old.loc[idx, "摘要"] = new_body.loc[idx].str[:300]
            imp = old.loc[idx][old.loc[idx, "分類"] == "歷史匯入"].index
            old.loc[imp, "發布時間"] = old.loc[imp, "連結"].map(by_link["發布時間"]).values
            updated += len(idx)
            retimed += len(imp)
            # 時間改了可能跨月：整檔重寫後，跨月的列交給下面 upsert 之外的搬移處理 (同月居多，直接留在原檔)
            tmp = path_m.with_suffix(".parquet.tmp")
            old.sort_values("發布時間", kind="stable").to_parquet(tmp, compression="zstd", index=False)
            tmp.replace(path_m)
    print(f"  庫內已有 → 內文更新 {updated:,} 則 (其中舊匯入列校正時間 {retimed:,} 則)")
    rows = df.assign(發布時間=df["發布時間"].dt.strftime("%Y-%m-%d %H:%M:%S")).to_dict("records")
    added = 0
    for i in range(0, len(rows), 50000):
        added += store.upsert(rows[i:i + 50000])
    print(f"  新增 {added:,} 則")

    # --- 重訊：只補我們開始收集之前的
    sys.path.insert(0, str(HERE.parent / "material_info"))
    import importlib
    ms = importlib.import_module("store") if False else None
    import importlib.util
    spec = importlib.util.spec_from_file_location("mstore", HERE.parent / "material_info" / "store.py")
    mstore = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mstore)
    m = pd.DataFrame(mops, columns=["公司代號", "公司簡稱", "t", "主旨", "說明"])
    m["t"] = pd.to_datetime(m["t"], errors="coerce", format="mixed")
    m = m.dropna(subset=["t"])
    m = m[m["t"] < pd.Timestamp(OWN_START)].drop_duplicates(["公司代號", "t", "主旨"])
    m["主旨"] = m["主旨"].str.replace(r"\s*\n\s*", "", regex=True)
    out = pd.DataFrame({
        "公司代號": m["公司代號"], "公司簡稱": m["公司簡稱"], "發布時間": m["t"].dt.strftime("%Y-%m-%d %H:%M:%S"),
        "主旨": m["主旨"], "說明": m["說明"], "市場別": "", "發言日期": m["t"].dt.strftime("%Y-%m-%d"),
        "發言時間": m["t"].dt.strftime("%H:%M:%S"), "符合條款": "", "事實發生日": "", "發言人": "", "發言人職稱": "",
        "發言人電話": "", "來源": "es_export", "MOPS鍵": "", "抓取時間": now})
    recs = out.to_dict("records")
    for i in range(0, len(recs), 20000):
        mstore.upsert(recs[i:i + 20000])
    print(f"重訊：匯入 {len(out):,} 筆 ({m['t'].min()} ~ {m['t'].max()})，說明空白 {(out['說明'] == '').sum():,}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
