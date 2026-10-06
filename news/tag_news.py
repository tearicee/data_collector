#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新聞事件標籤 (純規則，event_rules/rules.py)
  讀 D:/mops/news/data/新聞_YYYY-MM.parquet → 寫 D:/mops/news/tagged/新聞標籤_YYYY-MM.parquet
  欄位：連結、發布時間、來源、標題、個股代號、tags / modifiers (| 分隔)、entities (JSON)
  個股代號：鉅亨自帶；其他來源先抓內文「公司名（1234）」樣式，再用股票簡稱比對標題
  (3 字以上簡稱直接採計；2 字簡稱需在內文再出現 2 次以上，且不在易混淆名單內)。
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
from event_rules.rules import tag_text, themes_of  # noqa: E402

TAGGED_DIR = store.BASE_DIR / "tagged"
STOCK_INFO = "/mnt/d/finmind_data/TaiwanStockInfo/TaiwanStockInfo.parquet"
AMBIGUOUS = set("大陸 世界 數字 安心 全家 鳳凰 三星 東森 傳奇 互動 介面 建國 大中 大樹 天宇 乾坤 光譜 綠電 綠能 思源 永信 "
                "國產 全新 大量 統一 精華 聯合 亞洲 中華 第一 新產 台灣 日友 巨大 櫻花 同協 長虹 富強 鼎新 優美 和立 新華 "
                "全國 再生 南港 中石化 台船 力信 大同 華新 信義 太子 世紀 元山 普安 中聯 上奇 能率 高技 立康 百一 工信 順天 雙喜 佳總 青雲 金橋 精確 力士 華經 新興 星通 動力 昇陽".split())


def load_names() -> dict:
    try:
        d = pd.read_parquet(STOCK_INFO)
    except Exception:  # noqa: BLE001
        return {}
    d = d[d["stock_id"].str.fullmatch(r"[1-9]\d{3}") & d["type"].isin(["twse", "tpex"])]
    d = d.sort_values("date").drop_duplicates("stock_name", keep="last")
    return {n: c for n, c in zip(d["stock_name"].str.replace(r"[*\-]?(KY|DR|創)?$", "", regex=True), d["stock_id"])
            if len(n) >= 2 and n not in AMBIGUOUS}


NAMES = load_names()
CODE_NAMES = {c: n for n, c in NAMES.items()}
NAME_RE = re.compile("|".join(sorted(map(re.escape, NAMES), key=len, reverse=True))) if NAMES else None


def codes_from_names(title: str, body: str) -> list:
    if not NAME_RE:
        return []
    out = []
    for n in dict.fromkeys(NAME_RE.findall(title or "")):
        if len(n) >= 3 or (body or "").count(n) >= 2:
            out.append(NAMES[n])
    return out


CODE_RE = re.compile(r"[（(]\s*(\d{4,6}[A-Z]?)\s*(?:-TW|\.TW)?\s*[）)]")


def tag_month(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    rows = []
    for r in df.itertuples(index=False):
        t = tag_text(r.標題 or "", r.內文 or "", source="news")
        codes = [c for c in (r.個股代號 or "").split(",") if c]
        if not codes:
            by_name = codes_from_names(r.標題, r.內文)
            in_paren = [c for c in CODE_RE.findall(f"{r.標題}\n{r.內文 or ''}")
                        if not ("2020" <= c <= "2030") or c in by_name or c in CODE_NAMES
                        and CODE_NAMES[c] in (r.內文 or "")]   # (2026) 多半是年份，需公司名同時出現才算
            codes = list(dict.fromkeys(by_name + in_paren))[:8]
        th = themes_of(r.標題 or "", r.內文 or "")
        rows.append({
            "themes": "|".join(th["all"]), "title_themes": "|".join(th["title"]),
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
