#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新聞事件標籤 (純規則，event_rules/rules.py)
  讀 D:/mops/news/data/新聞_YYYY-MM.parquet → 寫 D:/mops/news/tagged/新聞標籤_YYYY-MM.parquet
  欄位：連結、發布時間、來源、標題、個股代號、tags / modifiers (| 分隔)、entities (JSON)
  個股代號：鉅亨自帶；其他來源從內文「公司名（1234）」樣式抽取。
用法：python tag_news.py                 全部月份重貼 (改過 rules.py 後用)
      python tag_news.py --changed       只處理新聞檔比標籤檔新的月份 (每日流程用，回補進來的舊月份也會補貼)
      python tag_news.py --months 2026-09 2026-08
"""
import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import store  # noqa: E402
from event_rules.rules import tag_text  # noqa: E402

TAGGED_DIR = store.BASE_DIR / "tagged"
CODE_RE = re.compile(r"[（(]\s*(\d{4,6}[A-Z]?)\s*(?:-TW|\.TW)?\s*[）)]")


def tag_month(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    rows = []
    for r in df.itertuples(index=False):
        t = tag_text(r.標題 or "", r.內文 or "", source="news")
        codes = [c for c in (r.個股代號 or "").split(",") if c]
        if not codes:
            codes = list(dict.fromkeys(CODE_RE.findall(f"{r.標題}\n{r.內文 or ''}")))[:8]
        rows.append({
            "連結": r.連結, "發布時間": r.發布時間, "來源": r.來源, "標題": r.標題,
            "個股代號": ",".join(codes), "tags": "|".join(t["tags"]), "title_tags": "|".join(t["title_tags"]),
            "modifiers": "|".join(t["modifiers"]),
            "entities": json.dumps(t["entities"], ensure_ascii=False) if t["entities"] else "",
        })
    out = pd.DataFrame(rows)
    TAGGED_DIR.mkdir(parents=True, exist_ok=True)
    dst = TAGGED_DIR / path.name.replace("新聞_", "新聞標籤_")
    tmp = dst.with_suffix(".parquet.tmp")
    out.to_parquet(tmp, compression="zstd", index=False)
    tmp.replace(dst)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", nargs="*")
    ap.add_argument("--changed", action="store_true")
    a = ap.parse_args()
    paths = sorted(store.DATA_DIR.glob("新聞_*.parquet"))
    if a.months:
        paths = [p for p in paths if p.stem[-7:] in a.months]
    if a.changed:
        def stale(p):
            dst = TAGGED_DIR / p.name.replace("新聞_", "新聞標籤_")
            return not dst.exists() or dst.stat().st_mtime < p.stat().st_mtime
        paths = [p for p in paths if stale(p)]
    total = tagged = 0
    for p in paths:
        out = tag_month(p)
        total += len(out)
        tagged += (out["tags"] != "").sum()
        print(f"{p.stem[-7:]} {len(out):>6,} 則，有標籤 {(out['tags'] != '').mean():.0%}，有個股 {(out['個股代號'] != '').mean():.0%}", flush=True)
    print(f"合計 {total:,} 則，有標籤 {tagged:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
