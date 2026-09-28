#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重大訊息查閱工具 (讀 D:/mops/material_info/data/*.parquet)
================================================================
範例：
  # 最近 7 天全部
  python query_material_info.py --days 7
  # 台積電 + 聯發科，含內文
  python query_material_info.py --code 2330 2454 --start 2026-09-01 --detail
  # 主旨或說明同時含「庫藏股」與「註銷」(關鍵字 AND；--any 改 OR；--subject-only 只搜主旨)
  python query_material_info.py --keyword 庫藏股 註銷 --start 2026-01-01
  # 匯出 CSV 給 Excel (utf-8-sig)
  python query_material_info.py --days 30 --market 上市 上櫃 --csv ~/重大訊息_近30天.csv
  # 推播用增量：抓取時間晚於游標的新訊息
  python query_material_info.py --fetched-after "2026-09-28 22:00:00" --detail

程式內使用 (策略研究)：
  import sys; sys.path.insert(0, "/home/tearicee/data_collector/material_info")
  import store; df = store.read_range("2026-01-01", "2026-09-30")
  # 或 DuckDB 直接查：SELECT * FROM '/mnt/d/mops/material_info/data/*.parquet' WHERE 主旨 LIKE '%澄清%'
"""

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store  # noqa: E402

BRIEF_COLS = ["發布時間", "公司代號", "公司簡稱", "市場別", "主旨"]


def main() -> int:
    ap = argparse.ArgumentParser(description="重大訊息查閱")
    ap.add_argument("--start", help="發言日期起 YYYY-MM-DD")
    ap.add_argument("--end", help="發言日期迄 YYYY-MM-DD")
    ap.add_argument("--days", type=int, help="最近 N 天 (含今天)，覆蓋 --start")
    ap.add_argument("--code", nargs="+", help="公司代號")
    ap.add_argument("--market", nargs="+", help="上市 / 上櫃 / 興櫃 / 公開發行")
    ap.add_argument("--keyword", nargs="+", help="關鍵字 (預設 AND)")
    ap.add_argument("--any", action="store_true", help="關鍵字改為 OR")
    ap.add_argument("--subject-only", action="store_true", help="只搜尋主旨")
    ap.add_argument("--fetched-after", help="只取抓取時間晚於此時間者 (增量/推播)")
    ap.add_argument("--detail", action="store_true", help="印出說明全文")
    ap.add_argument("--limit", type=int, default=200, help="終端機最多顯示筆數 (預設 200)")
    ap.add_argument("--csv", help="匯出 CSV 路徑 (utf-8-sig，Excel 可開)")
    args = ap.parse_args()

    start = args.start
    if args.days:
        start = (date.today() - timedelta(days=args.days - 1)).isoformat()
    df = store.read_range(start, args.end, args.fetched_after)

    if args.code:
        df = df[df["公司代號"].isin(args.code)]
    if args.market:
        df = df[df["市場別"].isin(args.market)]
    if args.keyword:
        text = df["主旨"] if args.subject_only else df["主旨"] + "\n" + df["說明"]
        masks = [text.str.contains(k, regex=False, na=False) for k in args.keyword]
        mask = masks[0]
        for m in masks[1:]:
            mask = (mask | m) if args.any else (mask & m)
        df = df[mask]

    if args.csv:
        out = Path(args.csv).expanduser()
        df.to_csv(out, index=False, encoding="utf-8-sig")
        print(f"已匯出 {len(df):,} 筆 → {out}")
        return 0

    print(f"共 {len(df):,} 筆" + (f"，顯示前 {args.limit} 筆" if len(df) > args.limit else ""))
    shown = df.head(args.limit)
    if args.detail:
        for _, r in shown.iterrows():
            print("─" * 60)
            print(f"{r['發布時間']}  {r['公司代號']} {r['公司簡稱']} ({r['市場別']})  {r['符合條款']}")
            print(f"主旨：{r['主旨']}")
            print(r["說明"])
    else:
        with pd.option_context("display.max_colwidth", 60, "display.width", 200,
                               "display.unicode.east_asian_width", True):
            print(shown[BRIEF_COLS].to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
