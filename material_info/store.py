#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重大訊息 Parquet 儲存層 (爬蟲 / 回補 / 查詢共用)
================================================================
  /mnt/d/mops/material_info/data/重大訊息_YYYY-MM.parquet
  - 依「發言日期」所屬月份分檔，zstd 壓縮 (實測約為 utf-8 CSV 的 1/3.7)。
  - 發布時間/抓取時間 存 timestamp，其餘文字欄存 string；市場別/來源 等重複值
    由 Parquet dictionary encoding 自動壓縮。
  - 每列唯一鍵：MOPS鍵 (enterDate-marketKind-公司代號-serialNumber)；
    OpenAPI 補入者無 MOPS鍵，改用 公司代號|發布時間。
  - upsert() 在跨行程寫入鎖內「重讀→合併→原子覆寫」，poll/daily/回補同時跑
    也不會互相蓋掉資料。
  - 推播/增量讀取：以「抓取時間」當游標 (read_range(fetched_after=...))。
"""

import fcntl
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

BASE_DIR = Path("/mnt/d/mops/material_info")
DATA_DIR = BASE_DIR / "data"
STATE_DIR = BASE_DIR / "state"
WRITE_LOCK = STATE_DIR / ".write.lock"  # 與 run_material_info.sh 的執行鎖 .lock 分開

# 前 5 欄為主要欄位，其餘為補充
FIELDS = [
    "公司代號", "公司簡稱", "發布時間", "主旨", "說明",
    "市場別", "發言日期", "發言時間", "符合條款", "事實發生日",
    "發言人", "發言人職稱", "發言人電話", "來源", "MOPS鍵", "抓取時間",
]
TS_FIELDS = ("發布時間", "抓取時間")
TS_FMT = "%Y-%m-%d %H:%M:%S"


def row_key(r: dict) -> str:
    return r.get("MOPS鍵") or f"{r['公司代號']}|{r['發布時間']}"


def month_path(ym: str) -> Path:
    return DATA_DIR / f"重大訊息_{ym}.parquet"


def load_month(ym: str) -> list:
    """讀某月為 dict 列表 (時間欄轉回 'YYYY-MM-DD HH:MM:SS' 字串)。"""
    path = month_path(ym)
    if not path.exists():
        return []
    df = pd.read_parquet(path)
    for c in TS_FIELDS:
        df[c] = df[c].dt.strftime(TS_FMT)
    return df.fillna("").to_dict("records")


def existing(ym: str) -> dict:
    """{row_key: row}，供爬蟲判斷哪些訊息已存。"""
    return {row_key(r): r for r in load_month(ym)}


def _write_month(ym: str, rows: list) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows).reindex(columns=FIELDS).fillna("")
    for c in FIELDS:
        df[c] = df[c].astype("string")
    for c in TS_FIELDS:
        df[c] = pd.to_datetime(df[c], format=TS_FMT, errors="coerce")
    df = df.sort_values(["發布時間", "公司代號", "MOPS鍵"], kind="stable")
    path = month_path(ym)
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, compression="zstd", index=False)
    tmp.replace(path)  # 原子性覆寫
    return path


@contextmanager
def write_lock():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(WRITE_LOCK, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def upsert(rows: list) -> list:
    """依月份合併寫入 (同鍵以新列取代)，回傳寫入的月份。"""
    by_month: dict = {}
    for r in rows:
        by_month.setdefault(r["發言日期"][:7], []).append(r)
    written = []
    with write_lock():
        for ym, new_rows in sorted(by_month.items()):
            merged = {row_key(r): r for r in load_month(ym)}
            merged.update({row_key(r): r for r in new_rows})
            _write_month(ym, list(merged.values()))
            written.append(ym)
    return written


def months_between(start: str, end: str) -> list:
    """'2026-08-28','2026-09-28' → ['2026-08','2026-09']"""
    return [p.strftime("%Y-%m") for p in pd.period_range(start[:7], end[:7], freq="M")]


def read_range(start: str | None = None, end: str | None = None,
               fetched_after: str | None = None) -> pd.DataFrame:
    """讀取發言日期區間 (含頭尾，YYYY-MM-DD) 為 DataFrame；未給區間讀全部。"""
    if start or end:
        paths = [month_path(ym) for ym in months_between(start or "2000-01-01",
                                                          end or "2100-12-31")]
        paths = [p for p in paths if p.exists()]
    else:
        paths = sorted(DATA_DIR.glob("重大訊息_*.parquet"))
    if not paths:
        return pd.DataFrame(columns=FIELDS)
    df = pd.concat([pd.read_parquet(p) for p in paths], ignore_index=True)
    if start:
        df = df[df["發言日期"] >= start]
    if end:
        df = df[df["發言日期"] <= end]
    if fetched_after:
        df = df[df["抓取時間"] > pd.Timestamp(fetched_after)]
    return df.sort_values("發布時間").reset_index(drop=True)
