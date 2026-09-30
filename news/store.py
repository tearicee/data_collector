#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新聞 Parquet 儲存層：/mnt/d/mops/news/data/新聞_YYYY-MM.parquet
  依發布時間月份分檔、zstd；唯一鍵 = 連結；upsert 在跨行程寫入鎖內「重讀→合併→原子覆寫」。
"""
import fcntl
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

BASE_DIR = Path("/mnt/d/mops/news")
DATA_DIR = BASE_DIR / "data"
STATE_DIR = BASE_DIR / "state"
FIELDS = ["發布時間", "來源", "分類", "標題", "摘要", "內文", "連結", "個股代號", "關鍵字", "抓取時間"]
TS_FIELDS = ("發布時間", "抓取時間")


def month_path(ym: str) -> Path:
    return DATA_DIR / f"新聞_{ym}.parquet"


@contextmanager
def write_lock():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / ".write.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def existing_links(ym: str) -> set:
    p = month_path(ym)
    return set(pd.read_parquet(p, columns=["連結"])["連結"]) if p.exists() else set()


def upsert(rows: list) -> int:
    """寫入新列 (已存在的連結保留舊列不覆寫)，回傳新增筆數。"""
    if not rows:
        return 0
    df = pd.DataFrame(rows).reindex(columns=FIELDS)
    for c in TS_FIELDS:
        df[c] = pd.to_datetime(df[c], errors="coerce")
    df = df.dropna(subset=["發布時間"])
    for c in FIELDS:
        if c not in TS_FIELDS:
            df[c] = df[c].fillna("").astype("string")
    added = 0
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with write_lock():
        for ym, g in df.groupby(df["發布時間"].dt.strftime("%Y-%m")):
            path = month_path(ym)
            old = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=FIELDS)
            new = g[~g["連結"].isin(set(old["連結"]))].drop_duplicates("連結")
            if new.empty:
                continue
            out = pd.concat([old, new], ignore_index=True).sort_values("發布時間", kind="stable")
            tmp = path.with_suffix(".parquet.tmp")
            out.to_parquet(tmp, compression="zstd", index=False)
            tmp.replace(path)
            added += len(new)
    return added


def read_range(start: str | None = None, end: str | None = None) -> pd.DataFrame:
    paths = sorted(DATA_DIR.glob("新聞_*.parquet"))
    if start:
        paths = [p for p in paths if p.stem[-7:] >= start[:7]]
    if end:
        paths = [p for p in paths if p.stem[-7:] <= end[:7]]
    if not paths:
        return pd.DataFrame(columns=FIELDS)
    df = pd.concat([pd.read_parquet(p) for p in paths], ignore_index=True)
    if start:
        df = df[df["發布時間"] >= pd.Timestamp(start)]
    if end:
        df = df[df["發布時間"] < pd.Timestamp(end) + pd.Timedelta(days=1)]
    return df.sort_values("發布時間").reset_index(drop=True)
