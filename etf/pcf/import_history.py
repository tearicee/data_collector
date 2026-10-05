#!/usr/bin/env python3
"""把「前 50 大 ETF 每日申購贖回 / 每日成分股」歷史壓縮檔匯入 PCF 資料集 (store.py)。

壓縮檔結構 (2026-10-02 那份):
    <資料夾>/前50大ETF每日申購贖回.csv            每檔每日一列
    <資料夾>/前50大ETF每日成分股.zip              內含 NN_<代號>_每日成分股.csv 共 50 檔
    <資料夾>/前50大ETF每日成分股_各檔覆蓋.csv      各檔涵蓋摘要 (不匯入，留在原始檔裡)

匯入時原欄位原值照收，只多算「資料基準日」「基準日依據」兩欄 (規則與依據見 store.py 開頭；
只有國內型推定得出來，海外與債券型留空)。
資料集裡已有的列 (收集器抓的) 不會被歷史檔蓋掉。可重複執行。

用法:
  python pcf/import_history.py <壓縮檔路徑>              # 匯入
  python pcf/import_history.py <壓縮檔路徑> --archive    # 匯入後把壓縮檔移到 D:\\etf_pcf\\source\\
  python pcf/import_history.py <壓縮檔路徑> --check      # 只比對資料集與壓縮檔的列數，不寫入
"""
from __future__ import annotations
import argparse
import io
import re
import shutil
import sys
import zipfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store  # noqa: E402


def _find(names: list[str], suffix: str) -> str:
    hits = [n for n in names if n.endswith(suffix)]
    if len(hits) != 1:
        raise ValueError(f"壓縮檔內找不到唯一的 *{suffix} (找到 {len(hits)} 個)")
    return hits[0]


def read_summary(z: zipfile.ZipFile) -> pd.DataFrame:
    raw = z.read(_find(z.namelist(), "每日申購贖回.csv"))
    return pd.read_csv(io.BytesIO(raw), encoding="utf-8-sig", dtype=str)


CONSTITUENT_NAME = re.compile(r"^\d{2}_(?P<code>[0-9A-Z]+)_每日成分股(_第\d+部分_.*)?\.csv$")


def constituent_files(z: zipfile.ZipFile):
    """逐檔產生 (代號, DataFrame)。成分 CSV 在巢狀 zip 裡，檔名 NN_<代號>_每日成分股.csv；
    太大的會拆成 NN_<代號>_每日成分股_第1部分_<起>至<迄>.csv 等數個 (如 00646)，在此合併。"""
    inner = zipfile.ZipFile(io.BytesIO(z.read(_find(z.namelist(), "每日成分股.zip"))))
    groups: dict[str, list[str]] = {}
    for n in sorted(inner.namelist()):
        m = CONSTITUENT_NAME.match(n.split("/")[-1])
        if m:
            groups.setdefault(m.group("code"), []).append(n)
    for code, names in groups.items():
        frames = [pd.read_csv(io.BytesIO(inner.read(n)), encoding="utf-8-sig", dtype=str, low_memory=False)
                  for n in names]
        yield code, pd.concat(frames, ignore_index=True)


def _apply_asof(df: pd.DataFrame, same_day: pd.Series, usable: pd.Series, cal: store.Calendar) -> pd.DataFrame:
    """usable 的列填推定的資料基準日 (same_day → 交易日，否則前一交易日)，其餘留空。"""
    prev = {d: cal.prev(d) for d in df["交易日"].dropna().unique()}
    asof = df["交易日"].where(same_day, df["交易日"].map(prev))
    df["資料基準日"] = asof.where(usable)
    df["基準日依據"] = pd.Series(store.BY_RULE, index=df.index).where(usable & asof.notna())
    return df


def add_summary_asof(df: pd.DataFrame, cal: store.Calendar, markets: dict[str, str]) -> pd.DataFrame:
    df = df.copy()
    usable = (df["代號"].map(markets) == store.DOMESTIC) & (df["狀態"] == store.HAS_DATA)
    return _apply_asof(df, df["投信"].isin(store.SUMMARY_SAME_DAY), usable, cal)


def add_constituent_asof(df: pd.DataFrame, cal: store.Calendar, markets: dict[str, str]) -> pd.DataFrame:
    df = df.copy()
    same = df["投信"].isin(store.CONSTITUENT_SAME_DAY) & ~df["資料來源"].fillna("").str.startswith(store.PCF_SOURCES)
    return _apply_asof(df, same, df["ETF代號"].map(markets) == store.DOMESTIC, cal)


def import_zip(zip_path: Path, root: Path | None = None, price_dir: Path | None = None,
               verbose: bool = True) -> dict:
    z = zipfile.ZipFile(zip_path)
    hist = read_summary(z)
    # 歷史檔每檔每個開市日都有一列 (來源沒資料的也有，狀態為「來源無資料」)，
    # 0050 又從 2003 年起涵蓋全部開市日，所以它自己的交易日就是完整的交易日曆
    cal = store.Calendar(set(hist["交易日"].dropna()) | set(store.price_days(price_dir)))
    u = store.load_universe()
    markets = dict(zip(u["代號"], u["市場"]))
    hist = store.normalize_summary(add_summary_asof(hist, cal, markets))

    existing = store.load_summary(root)
    merged = pd.concat([hist, existing], ignore_index=True).drop_duplicates(["代號", "交易日"], keep="last")
    merged = merged.sort_values(["市值排名", "代號", "交易日"], kind="stable").reset_index(drop=True)
    store._atomic_write(merged, store.summary_path(root))
    stats = {"summary_rows": len(merged), "summary_from_zip": len(hist),
             "summary_range": (str(hist["交易日"].min()), str(hist["交易日"].max())),
             "constituent_rows": 0, "constituent_files": 0}
    if verbose:
        print(f"[import] 申購贖回 {len(hist):,} 列 ({stats['summary_range'][0]} ~ {stats['summary_range'][1]}) "
              f"→ 資料集共 {len(merged):,} 列")

    for code, df in constituent_files(z):
        new = store.normalize_constituents(add_constituent_asof(df, cal, markets))
        old = store.load_constituents(code, root)
        keep = old[~old["交易日"].isin(set(new["交易日"]))]           # 歷史檔沒有的日子 (收集器抓的) 保留
        out = pd.concat([new, keep], ignore_index=True).sort_values("交易日", kind="stable").reset_index(drop=True)
        store._atomic_write(out, store.constituent_path(code, root))
        stats["constituent_rows"] += len(new)
        stats["constituent_files"] += 1
        if verbose:
            print(f"[import] 成分 {code}: {len(new):,} 列, {new['交易日'].nunique():,} 天 "
                  f"({new['交易日'].min()} ~ {new['交易日'].max()})")
    if verbose:
        print(f"[import] 完成: 成分 {stats['constituent_files']} 檔 共 {stats['constituent_rows']:,} 列")
    return stats


def check(zip_path: Path, root: Path | None = None) -> int:
    """資料集是否完整包含壓縮檔的內容 (列數、原欄位值)。回傳不一致的項目數。"""
    z = zipfile.ZipFile(zip_path)
    bad = 0
    hist = store.normalize_summary(read_summary(z))
    cur = store.load_summary(root)
    cols = [c for c in store.SUMMARY_COLUMNS if c not in store.ADDED_COLUMNS]
    j = hist[cols].merge(cur[cols], on=cols, how="left", indicator=True)
    miss = int((j["_merge"] != "both").sum())
    print(f"[check] 申購贖回: 壓縮檔 {len(hist):,} 列, 資料集 {len(cur):,} 列, 壓縮檔有而資料集不同/缺 {miss} 列")
    bad += bool(miss)
    total = 0
    for code, df in constituent_files(z):
        new = store.normalize_constituents(df)
        old = store.load_constituents(code, root)
        old = old[old["交易日"].isin(set(new["交易日"]))]
        cc = [c for c in store.CONSTITUENT_COLUMNS if c not in store.ADDED_COLUMNS]
        a = new[cc].astype("string").fillna("").sort_values(cc, kind="stable").reset_index(drop=True)
        b = old[cc].astype("string").fillna("").sort_values(cc, kind="stable").reset_index(drop=True)
        same = len(a) == len(b) and a.equals(b)
        total += len(new)
        if not same:
            bad += 1
            print(f"[check] 成分 {code} 不一致: 壓縮檔 {len(a):,} 列 vs 資料集 {len(b):,} 列")
    print(f"[check] 成分: 壓縮檔共 {total:,} 列, 不一致 {bad - bool(miss)} 檔")
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="匯入前 50 大 ETF 申購贖回/成分股歷史壓縮檔")
    ap.add_argument("zip", type=Path, help="歷史壓縮檔路徑")
    ap.add_argument("--root", type=Path, default=None, help=f"資料集目錄 (預設 {store.PCF_DIR})")
    ap.add_argument("--archive", action="store_true", help="匯入後把壓縮檔移到 <資料集>/source/")
    ap.add_argument("--check", action="store_true", help="只比對，不寫入")
    args = ap.parse_args(argv)
    if args.check:
        return 1 if check(args.zip, args.root) else 0
    import_zip(args.zip, args.root)
    if args.archive:
        dest = (args.root or store.PCF_DIR) / "source"
        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(str(args.zip), str(dest / args.zip.name))
        print(f"[import] 壓縮檔已移到 {dest / args.zip.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
