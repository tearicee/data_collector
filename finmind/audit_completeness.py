#!/usr/bin/env python3
"""FinMind 資料完整性稽核 — 比對交易日曆與 D 槽實際落地檔案，找出真正的缺漏。

直接拿交易日曆逐日比對會產生大量假警報，本工具處理三層雜訊：

  1. 臨時休市日 — 颱風假等，行事曆不會回填 (見 common/market_closures.py)
  2. 資料集頻率 — 季/月/週頻與快照型資料集本來就不是每個交易日一檔
  3. 尚未入庫    — 當日資料多在盤後 16:00~隔日 05:00 分批落地，
                   最新一個交易日未到齊屬正常，另列為「待入庫」不算缺漏

用法:
  python finmind/audit_completeness.py                # 近 25 個交易日
  python finmind/audit_completeness.py --days 60      # 自訂區間
  python finmind/audit_completeness.py --all          # 連正常的也列出
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import market_closures  # noqa: E402

ROOT = Path(r"D:\finmind_data") if sys.platform == "win32" else Path("/mnt/d/finmind_data")
TRADING_DATE_PARQUET = ROOT / "TaiwanStockTradingDate" / "TaiwanStockTradingDate.parquet"

# 非日頻資料集 — 缺「每個交易日」屬正常，只檢查是否在合理時間內有更新。
QUARTERLY = {
    "TaiwanStockBalanceSheet",
    "TaiwanStockCashFlowsStatement",
    "TaiwanStockFinancialStatements",
}
MONTHLY = {"TaiwanStockMonthRevenue", "TaiwanStockMonthPrice"}
WEEKLY = {"TaiwanStockWeekPrice", "TaiwanStockHoldingSharesPer"}

# 以 by_stock/ 逐股票存檔，檔名為股票代號而非日期，無法按日比對。
BY_STOCK = {
    "TaiwanStockDividend",
    "TaiwanStockDividendResult",
    "TaiwanStockMarketValueWeight",
}

CADENCE_NOTE = {
    "quarterly": "季頻",
    "monthly": "月頻",
    "weekly": "週頻",
    "by_stock": "逐股票存檔",
    "snapshot": "快照",
}


def cadence_of(ds: str, has_dated_files: bool) -> str:
    if ds in BY_STOCK:
        return "by_stock"
    if ds in QUARTERLY:
        return "quarterly"
    if ds in MONTHLY:
        return "monthly"
    if ds in WEEKLY:
        return "weekly"
    return "daily" if has_dated_files else "snapshot"


def expected_trading_days(today: date, n: int) -> list[str]:
    """回傳今天(含)以前最近 n 個交易日，已扣除週末與臨時休市。"""
    df = pd.read_parquet(TRADING_DATE_PARQUET)
    days = sorted(d for d in df["date"].astype(str) if d <= today.isoformat())
    kept, _ = market_closures.filter_trading_days(days)
    return [str(d) for d in kept][-n:]


def dates_present(files: list[str]) -> set[str]:
    out = set()
    for f in files:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(f))
        if m:
            out.add(m.group(1))
    return out


def newest_mtime(files: list[str]) -> str:
    if not files:
        return "—"
    p = max(files, key=os.path.getmtime)
    return pd.Timestamp(os.path.getmtime(p), unit="s", tz="Asia/Taipei").strftime("%Y-%m-%d %H:%M")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=25, help="稽核最近幾個交易日 (預設 25)")
    ap.add_argument("--all", action="store_true", help="連正常資料集也列出")
    ap.add_argument("--today", default="", help="覆寫今天日期 (YYYY-MM-DD，測試用)")
    args = ap.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()
    recent = expected_trading_days(today, args.days)
    if not recent:
        print("找不到交易日資料，請確認 TaiwanStockTradingDate.parquet")
        return 2
    latest = recent[-1]

    print(f"稽核區間 {recent[0]} ~ {latest}（{len(recent)} 個交易日）")
    closures = [d for d in market_closures.UNSCHEDULED_CLOSURES if recent[0] <= d <= latest]
    if closures:
        for c in sorted(closures):
            print(f"  已排除臨時休市：{c} — {market_closures.UNSCHEDULED_CLOSURES[c]}")
    print()

    gaps: list[tuple[str, list[str]]] = []
    pending: list[str] = []
    normal: list[str] = []

    for ds in sorted(os.listdir(ROOT)):
        d = ROOT / ds
        if not d.is_dir() or ds == "logs":
            continue
        dated = glob.glob(f"{d}/*/*.parquet")
        allf = glob.glob(f"{d}/**/*.parquet", recursive=True)
        cad = cadence_of(ds, bool(dated))

        if cad != "daily":
            note = CADENCE_NOTE[cad]
            normal.append(f"[--] {ds:<50} {note}，最後更新={newest_mtime(allf)} 檔數={len(allf)}")
            continue

        have = dates_present(dated)
        miss = [x for x in recent if x not in have]
        # 最新交易日尚未入庫 → 待入庫，不算缺漏
        if miss == [latest]:
            pending.append(f"[..] {ds:<50} {latest} 待入庫")
            continue
        if miss and miss[-1] == latest:
            miss = miss[:-1]
            pending.append(f"[..] {ds:<50} {latest} 待入庫")
        if miss:
            gaps.append((ds, miss))
        else:
            normal.append(f"[OK] {ds:<50} {len(recent)}/{len(recent)} 完整")

    if gaps:
        print("=" * 72)
        print(f"⚠️  真實缺漏（{len(gaps)} 個資料集）")
        print("=" * 72)
        for ds, miss in gaps:
            print(f"[!!] {ds}  缺 {len(miss)} 日")
            print(f"     {', '.join(miss)}")
    else:
        print("✅ 無缺漏：所有日頻資料集在扣除休市日後皆完整")

    if pending:
        print(f"\n[待入庫] 最新交易日 {latest} 尚未落地（盤後分批下載中，屬正常）")
        for line in pending:
            print(" ", line)

    if args.all and normal:
        print("\n[其餘資料集]")
        for line in normal:
            print(" ", line)

    print(f"\n小結：缺漏 {len(gaps)}｜待入庫 {len(pending)}｜正常 {len(normal)}")
    return 1 if gaps else 0


if __name__ == "__main__":
    raise SystemExit(main())
