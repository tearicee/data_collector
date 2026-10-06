"""告警發送紀錄 (兩個專案共用同一份檔)。

Discord 告警送出去就沒了、心跳又會被下一輪成功覆蓋，事後查「今天有哪些告警」沒有依據，
所以每次送告警時在 /mnt/d/monitoring/alerts.jsonl 追加一行 (daily_script 的
src/core/alert_log.py 寫同一份)。這裡也放「把 log 尾段縮成告警用」的規則，讓告警內容保持簡潔。

查詢：python common/alert_log.py [--hours 24]
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

if sys.platform == "win32":
    ALERT_LOG = Path(r"D:\monitoring\alerts.jsonl")
else:
    ALERT_LOG = Path("/mnt/d/monitoring/alerts.jsonl")

TAIL_LINES = 12          # 告警最多帶幾行 log
TAIL_CHARS = 800         # 告警 log 尾段最多幾個字
# 進度／等待／逐檔複製這類行只是雜訊，告警裡不帶
NOISE_RE = re.compile(r"進度|等待|Copied \(new\)|Updated modification time|^\s*$")


def compact_tail(text: str, lines: int = TAIL_LINES, chars: int = TAIL_CHARS) -> str:
    """去掉進度雜訊、只留最後幾行，再從尾端截到字數上限。"""
    kept = [ln.rstrip() for ln in text.splitlines() if not NOISE_RE.search(ln)]
    out = "\n".join(kept[-lines:])
    if len(out) > chars:
        out = "…" + out[-chars:]
    return out


def record(source: str, job: str, message: str, exit_code: int | None = None,
           log_tail: str = "", sent: bool | None = None, path: Path = ALERT_LOG) -> None:
    """追加一筆告警紀錄；寫不進去只印錯誤，不影響告警本身。"""
    row = {"ts": datetime.now().isoformat(timespec="seconds"), "source": source, "job": job,
           "exit_code": exit_code, "message": message.strip()[:300],
           "tail": compact_tail(log_tail), "sent": sent}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[alert_log] 寫入失敗: {e}", file=sys.stderr)


def read(since: datetime | None = None, path: Path = ALERT_LOG) -> list[dict]:
    """讀出 since 之後的告警 (時間由舊到新)。"""
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            try:
                row = json.loads(ln)
            except ValueError:
                continue
            if since is None or row.get("ts", "") >= since.isoformat(timespec="seconds"):
                rows.append(row)
    return rows


def summary_lines(rows: list[dict]) -> list[str]:
    out = []
    for r in rows:
        exit_s = "" if r.get("exit_code") is None else f" (exit={r['exit_code']})"
        out.append(f"  {r['ts'][5:16]}  [{r.get('source', '?')}] {r.get('job', '?')}{exit_s}"
                   f"  {str(r.get('message', ''))[:80]}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="列出最近的告警紀錄")
    ap.add_argument("--hours", type=float, default=24, help="往回幾小時 (預設 24)")
    ap.add_argument("--tail", action="store_true", help="連 log 尾段一起印")
    args = ap.parse_args()
    rows = read(datetime.now() - timedelta(hours=args.hours))
    if not rows:
        print(f"最近 {args.hours:g} 小時沒有告警紀錄 ({ALERT_LOG})")
        return 0
    print(f"最近 {args.hours:g} 小時 {len(rows)} 筆告警：")
    for r, line in zip(rows, summary_lines(rows)):
        print(line)
        if args.tail and r.get("tail"):
            print("      " + r["tail"].replace("\n", "\n      "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
