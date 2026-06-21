#!/usr/bin/env python3
"""FinMind 各資料集下載進度查詢。

用法：
    python backfill_progress.py        # 或 ./venv/bin/python backfill_progress.py
"""
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path("/mnt/d/finmind_data")
TODAY = date.today()

# (dataset, start, end, rule)  rule: 'weekday'=週一~五, 'no_sunday'=週一~六
DATASETS = [
    ("TaiwanStockPriceTick",          date(2024, 1, 5),  date(2026, 5, 31), "weekday"),
    ("TaiwanStockTradingDailyReport", date(2021, 6, 30), TODAY,             "weekday"),
    ("TaiwanFuturesTick",             date(2019, 1, 1),  TODAY,             "no_sunday"),
    ("TaiwanOptionTick",              date(2019, 1, 1),  TODAY,             "no_sunday"),
]


def candidate_days(s, e, rule):
    n, d = 0, s
    while d <= e:
        wd = d.weekday()
        if (rule == "weekday" and wd < 5) or (rule == "no_sunday" and wd != 6):
            n += 1
        d += timedelta(days=1)
    return n


def human(b):
    b = float(b)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f}{u}"
        b /= 1024
    return f"{b:.1f}PB"


def scan(dataset):
    dd = ROOT / dataset
    files = list(dd.glob("*/*.parquet")) if dd.exists() else []
    dates = sorted(f.stem.rsplit("_", 1)[-1] for f in files)
    size = sum(f.stat().st_size for f in files)
    return len(files), dates, size


print(f"==== FinMind 下載進度  ({datetime.now():%Y-%m-%d %H:%M:%S}) ====\n")
total = 0
for name, s, e, rule in DATASETS:
    done, dates, size = scan(name)
    total += size
    cand = candidate_days(s, e, rule)
    pct = done / cand * 100 if cand else 0
    bar = "#" * int(pct / 5) + "-" * (20 - int(pct / 5))
    span = f"{dates[0]} ~ {dates[-1]}" if dates else "—"
    print(f"[{name}]")
    print(f"  範圍 {s}~{e} ({rule})  候選日 {cand}")
    print(f"  [{bar}] {pct:5.1f}%  已下載 {done}/{cand} 檔  {human(size)}")
    print(f"  已涵蓋 {span}" + (f"  (reverse 下載中，最舊已達 {dates[0]})" if dates else ""))
    print()

print(f"總計大小: {human(total)}\n")

print("==== 執行中的下載程序 ====")
out = subprocess.run(["pgrep", "-af", r"download_.*tick\.py"],
                     capture_output=True, text=True).stdout
lines = [l for l in out.splitlines() if "venv/bin/python" in l]
if lines:
    for l in lines:
        # 只顯示 pid + 指令尾段，避免太長
        parts = l.split(None, 1)
        print(f"  pid {parts[0]}: ...{parts[1][-90:]}" if len(parts) > 1 else f"  {l}")
else:
    print("  (目前沒有 backfill 下載程序在執行)")

print("\n提示：啟動 ./start_finmind_backfill.sh | 停止 ./stop_finmind_backfill.sh")
