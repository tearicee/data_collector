#!/usr/bin/env python3
"""任務心跳 (heartbeat) 狀態檔。

每個爬蟲任務結束時寫一份 <job>.json，記錄最後執行時間、成功/失敗、
訊息與資料統計。每日健檢 (daily_healthcheck.py) 讀這些檔判斷各任務是否
正常。純本機檔案 (不備份到雲端)。

檔案位置: D:\\monitoring\\heartbeat\\<job>.json  (WSL: /mnt/d/monitoring/heartbeat)

CLI (供 shell wrapper 使用):
  python common/heartbeat.py <job> --status ok|fail --exit-code 0 --message "..."
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    HEARTBEAT_DIR = Path(r"D:\monitoring\heartbeat")
else:
    HEARTBEAT_DIR = Path("/mnt/d/monitoring/heartbeat")

STATUS_OK = "ok"
STATUS_FAIL = "fail"


CADENCES = ("daily", "weekly", "monthly", "yearly")


def write(job: str, status: str = STATUS_OK, exit_code: int = 0,
          message: str = "", stats: dict | None = None,
          merge_stats: bool = False, cadence: str | None = None) -> Path:
    """寫入 (覆寫) 某任務的心跳檔，回傳檔案路徑。

    merge_stats=True 且未提供 stats 時，沿用現有心跳檔的 stats。用於外層
    wrapper 更新執行狀態 (status/exit_code) 但保留 python 任務先寫入的資料
    統計 (stats)，避免互相覆蓋。

    cadence 為任務的預期週期 (daily/weekly/monthly/yearly)，每日健檢據此判斷
    是否逾期；未給時沿用現有心跳檔的 cadence，健檢端再退回預設表。
    """
    HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
    prev = read(job) if (merge_stats or cadence is None) else None
    if stats is None and merge_stats:
        stats = (prev or {}).get("stats") if prev else None
    if cadence is None and prev:
        cadence = prev.get("cadence")
    if cadence is not None and cadence not in CADENCES:
        raise ValueError(f"cadence 必須是 {CADENCES} 之一: {cadence}")
    payload = {
        "job": job,
        "last_run": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "exit_code": exit_code,
        "message": message,
        "stats": stats or {},
    }
    if cadence:
        payload["cadence"] = cadence
    path = HEARTBEAT_DIR / f"{job}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)                            # 原子替換，避免讀到半寫檔
    return path


def read(job: str) -> dict | None:
    path = HEARTBEAT_DIR / f"{job}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                # noqa: BLE001
        return None


def read_all() -> dict[str, dict]:
    """回傳 {job: payload} 全部心跳。"""
    out: dict[str, dict] = {}
    if not HEARTBEAT_DIR.exists():
        return out
    for p in sorted(HEARTBEAT_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            out[data.get("job", p.stem)] = data
        except Exception:                            # noqa: BLE001
            continue
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="寫入任務心跳狀態檔")
    ap.add_argument("job", help="任務名稱 (檔名)")
    ap.add_argument("--status", default=STATUS_OK, choices=[STATUS_OK, STATUS_FAIL])
    ap.add_argument("--exit-code", type=int, default=0)
    ap.add_argument("--message", default="")
    ap.add_argument("--merge-stats", action="store_true",
                    help="沿用現有心跳檔的 stats (只更新執行狀態)")
    ap.add_argument("--cadence", choices=CADENCES,
                    help="任務預期週期 (健檢依此判斷逾期); 未給沿用現有心跳檔")
    args = ap.parse_args()
    path = write(args.job, status=args.status, exit_code=args.exit_code,
                 message=args.message, merge_stats=args.merge_stats,
                 cadence=args.cadence)
    print(f"心跳已寫入 {path}")
