#!/usr/bin/env python3
"""每日健檢摘要。

彙整兩部分並發一則 Discord 摘要:
  A. 任務心跳 — 讀 common/heartbeat 的所有 <job>.json，列出各任務最後執行
     時間、成功/失敗，超過門檻未更新者標記為 stale。
  B. 資料落地 — 直接掃 D 槽關鍵資料夾，回報「最新一天」的檔數，確認資料
     真的有進來 (即使該任務尚未接心跳 wrapper 也能反映)。

用法:
  python common/daily_healthcheck.py           # 送 Discord
  python common/daily_healthcheck.py --dry      # 只印，不送 (測試用)
"""
from __future__ import annotations
import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import heartbeat, notify_discord  # noqa: E402

if sys.platform == "win32":
    D = Path(r"D:\\")
else:
    D = Path("/mnt/d")

ETF_DATA = D / "etf_daily_holdings" / "data"
FUND_MASTER = D / "etf_daily_holdings" / "fund_master"
RAW_DIR = D / "etf_daily_holdings_raw"

STALE_HOURS = 26            # 心跳超過此時數未更新視為 stale


def _latest_day_count(directory: Path, pattern: str = "*.csv") -> tuple[str, int]:
    """回傳 (最新日期字串, 該日檔數)。檔名格式假設為 YYYYMMDD_*.csv。"""
    if not directory.exists():
        return ("—", 0)
    dates: dict[str, int] = {}
    for p in directory.glob(pattern):
        m = re.match(r"(\d{8})_", p.name)
        if m:
            dates[m.group(1)] = dates.get(m.group(1), 0) + 1
    if not dates:
        return ("—", 0)
    latest = max(dates)
    return (latest, dates[latest])


def _raw_latest_count() -> tuple[str, int]:
    """raw 資料夾為 <發行商>/YYYYMMDD_<code>.csv，遞迴統計最新日期檔數。"""
    if not RAW_DIR.exists():
        return ("—", 0)
    dates: dict[str, int] = {}
    for p in RAW_DIR.glob("**/*.csv"):
        m = re.match(r"(\d{8})_", p.name)
        if m:
            dates[m.group(1)] = dates.get(m.group(1), 0) + 1
    if not dates:
        return ("—", 0)
    latest = max(dates)
    return (latest, dates[latest])


def build_report() -> tuple[str, bool]:
    """回傳 (報表文字, 是否有異常)。"""
    now = datetime.now()
    lines: list[str] = []
    problems: list[str] = []

    lines.append(f"每日健檢摘要  {now:%Y-%m-%d %H:%M}")
    lines.append("=" * 46)

    # ---- A. 任務心跳 ----
    lines.append("\n[任務心跳]")
    hbs = heartbeat.read_all()
    if not hbs:
        lines.append("  (尚無任何心跳檔)")
    for job, hb in sorted(hbs.items()):
        last = hb.get("last_run", "")
        status = hb.get("status", "?")
        stale = False
        try:
            dt = datetime.fromisoformat(last)
            stale = (now - dt) > timedelta(hours=STALE_HOURS)
        except Exception:                            # noqa: BLE001
            stale = True
        mark = "OK "
        if status != "ok":
            mark = "FAIL"; problems.append(f"{job} 失敗")
        elif stale:
            mark = "STALE"; problems.append(f"{job} 逾 {STALE_HOURS}h 未更新")
        stats = hb.get("stats") or {}
        stats_s = ("  " + json.dumps(stats, ensure_ascii=False)) if stats else ""
        lines.append(f"  [{mark:<5}] {job:<20} {last}{stats_s}")

    # ---- B. 資料落地 ----
    lines.append("\n[資料落地 (最新一天檔數)]")
    cm_day, cm_n = _latest_day_count(ETF_DATA)
    lines.append(f"  CMoney 持股      {cm_day}  {cm_n} 檔")
    if cm_n == 0:
        problems.append("CMoney 無任何資料")

    raw_day, raw_n = _raw_latest_count()
    lines.append(f"  投信原始持股      {raw_day}  {raw_n} 檔")

    # 主檔狀態
    latest_master = FUND_MASTER / "fund_master_latest.csv"
    codes_file = FUND_MASTER / "fund_codes.json"
    if latest_master.exists():
        mtime = datetime.fromtimestamp(latest_master.stat().st_mtime)
        n_codes = 0
        if codes_file.exists():
            try:
                n_codes = len(json.loads(codes_file.read_text(encoding="utf-8")))
            except Exception:                        # noqa: BLE001
                pass
        lines.append(f"  MOPS 基金主檔     更新於 {mtime:%Y-%m-%d %H:%M}  {n_codes} 檔代號")
    else:
        lines.append("  MOPS 基金主檔     (尚未產生)")

    # unsupported / 待補 adapter (若檔案存在)
    unsupported = FUND_MASTER / "unsupported.json"
    if unsupported.exists():
        try:
            us = json.loads(unsupported.read_text(encoding="utf-8"))
            lines.append(f"  CMoney 無資料代號  {len(us)} 檔")
        except Exception:                            # noqa: BLE001
            pass
    pending = RAW_DIR / "pending_adapters.json"
    if pending.exists():
        try:
            pa = json.loads(pending.read_text(encoding="utf-8"))
            if pa:
                issuers = sorted(set(pa.values())) if isinstance(pa, dict) else []
                lines.append(f"  待補投信 adapter   {len(pa)} 檔 / {len(issuers)} 家: {issuers}")
        except Exception:                            # noqa: BLE001
            pass

    # ---- 結論 ----
    lines.append("\n" + "=" * 46)
    has_problem = bool(problems)
    if has_problem:
        lines.insert(0, f"🔴 健檢發現 {len(problems)} 項異常")
        lines.append("異常項目: " + "; ".join(problems))
    else:
        lines.insert(0, "🟢 所有任務正常")
    return ("\n".join(lines), has_problem)


def main():
    ap = argparse.ArgumentParser(description="每日健檢摘要")
    ap.add_argument("--dry", action="store_true", help="只印出，不送 Discord")
    ap.add_argument("--now", action="store_true", help="(相容用) 立即執行")
    args = ap.parse_args()

    report, has_problem = build_report()
    print(report)

    if args.dry:
        return

    try:
        # 標題行 (帶 emoji) 不包框，其餘報表包程式碼框
        first, rest = report.split("\n", 1)
        notify_discord.post(first, code_block=False)
        notify_discord.post(rest, code_block=True)
    except Exception as e:                           # noqa: BLE001
        print(f"[healthcheck][error] 送出摘要失敗: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
