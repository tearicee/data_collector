#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
整理人工標註檔 重要新聞.csv → /mnt/d/mops/news/labels/重要新聞_clean.parquet
  - 去空列與殘留欄 (第 9、14 欄是類型清單，不是資料)
  - 63 種 type 合併為 category；對應 news_prompt 的新鮮度標籤 (原 4 類 + 新增 2 類)
  - 美股代號統一為 XXX.US；同一則新聞多檔股票以 news_id 串起
用途：LLM few-shot 範例、回測標準答案。
"""
import hashlib
import sys
from pathlib import Path

import pandas as pd

SRC = Path("/home/tearicee/daily_script/data/重要新聞.csv")
OUT = Path("/mnt/d/mops/news/labels/重要新聞_clean.parquet")

CATEGORY = {  # category: [原 type...]
    "供應鏈利多": ["供應鏈利多", "訂單", "取得訂單", "取得認證", "新產品", "新業務", "題材", "展覽", "擴廠"],
    "供應鏈利空": ["供應鏈利空", "產業衰退", "破產", "訴訟"],
    "供需/報價": ["供需失衡", "漲價", "缺貨"],
    "政策": ["美國政策", "台灣政策", "中國政策", "歐洲政策", "韓國政策", "政策", "政要", "選舉", "聯準會利率", "外匯"],
    "戰爭/災害": ["戰爭", "災害", "罷工"],
    "指數調整": ["指數調整"],
    "併購/經營權": ["併購", "經營權", "私募"],
    "籌資": ["現金增資", "增資"],
    "股權異動": ["申報轉讓", "資產處分", "處分", "13F", "大額投資人"],
    "庫藏股": ["庫藏股", "買回股份"],
    "財報/營收": ["財報", "營收公告", "營收", "自結", "法說會", "重要數據"],
    "股利": ["股利公告", "股東會", "拆股"],
    "生技": ["新藥解盲", "生技藥證", "藥證", "藥證申請", "生技上市審查"],
    "機構觀點": ["機構報告", "機構降評", "評等"],
    "其他": ["掛牌", "內線交易", "檢調搜索", "加密貨幣"],
}
FRESHNESS = {  # category → news_prompt 新鮮度標籤
    "供應鏈利多": "供應鏈核爆", "供應鏈利空": "供應鏈核爆", "供需/報價": "供應鏈核爆",
    "政策": "政策/環境反轉", "戰爭/災害": "政策/環境反轉",
    "指數調整": "預期落差", "財報/營收": "極端異常", "股利": "極端異常", "生技": "預期落差",
    "併購/經營權": "資本/股權事件", "籌資": "資本/股權事件", "股權異動": "資本/股權事件",
    "庫藏股": "資本/股權事件", "機構觀點": "預期落差", "其他": "",
}
TYPE2CAT = {t: c for c, ts in CATEGORY.items() for t in ts}
US_FIX = {"NVDA": "NVDA.US"}


def main() -> int:
    df = pd.read_csv(SRC, dtype=str, keep_default_na=False).iloc[:, :8]
    df = df[df["日期"].str.strip() != ""].copy()
    df = df.apply(lambda c: c.str.strip())
    df["stock_id"] = df["stock_id"].replace(US_FIX)
    unknown = sorted(set(df["type"]) - set(TYPE2CAT))
    if unknown:
        print(f"[WARN] 未對應的 type: {unknown}")
    df["category"] = df["type"].map(TYPE2CAT).fillna("其他")
    df["freshness_tag"] = df["category"].map(FRESHNESS)
    df["market"] = "TW"
    df.loc[df["stock_id"].str.endswith(".US"), "market"] = "US"
    df.loc[~df["stock_id"].str.match(r"^\d") & (df["market"] == "TW"), "market"] = "INDEX"
    df["news_id"] = [hashlib.md5(f"{d}|{t}".encode()).hexdigest()[:12]
                     for d, t in zip(df["日期"], df["title"])]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT, compression="zstd", index=False)
    print(f"{len(df)} 筆 / {df['news_id'].nunique()} 則新聞 → {OUT}")
    print(df["category"].value_counts().to_string())
    print(df["freshness_tag"].value_counts().to_string())
    print("direction 空白:", (df["direction"] == "").sum())
    return 0


if __name__ == "__main__":
    sys.exit(main())
