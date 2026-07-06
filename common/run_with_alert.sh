#!/bin/bash
# ============================================================
# 通用 cron 任務包裝器
#   用法: run_with_alert.sh <job-name> -- <command...>
#
# 功能:
#   1. 執行 command，輸出同時顯示並存入暫存 log
#   2. 捕捉 exit code，寫入執行層心跳 (common/heartbeat.py)
#   3. 失敗時透過 Discord 發即時告警 (帶 job 名 + log 尾段)
#
# 範例:
#   run_with_alert.sh mops_fund_list -- "$VENV_PY" etf/mops_fund_list.py --now
# ============================================================
set -u

DC_ROOT=/home/tearicee/data_collector
VENV_PY="$DC_ROOT/.venv/bin/python"

JOB="${1:-unknown}"; shift || true
if [ "${1:-}" = "--" ]; then shift; fi

if [ "$#" -eq 0 ]; then
    echo "用法: run_with_alert.sh <job-name> -- <command...>" >&2
    exit 2
fi

TMPLOG="$(mktemp)"
trap 'rm -f "$TMPLOG"' EXIT

# 執行命令：stdout+stderr 一併顯示並存 tmp log；rc 取 pipe 第一個指令的 exit code
"$@" 2>&1 | tee "$TMPLOG"
rc=${PIPESTATUS[0]}

if [ "$rc" -eq 0 ]; then STATUS=ok; else STATUS=fail; fi

# 執行層心跳 (--merge-stats: 保留 python 任務先寫入的資料統計)
"$VENV_PY" "$DC_ROOT/common/heartbeat.py" "$JOB" \
    --status "$STATUS" --exit-code "$rc" --merge-stats \
    --message "$(if [ $rc -eq 0 ]; then echo '執行成功'; else echo "執行失敗 (exit=$rc)"; fi)" \
    >/dev/null 2>&1 || true

# 失敗即時告警 (log 尾段 1500 bytes)
if [ "$rc" -ne 0 ]; then
    TAIL="$(tail -c 1500 "$TMPLOG")"
    "$VENV_PY" -c "
import sys
sys.path.insert(0, '$DC_ROOT')
from common import notify_discord
notify_discord.alert(sys.argv[1], sys.argv[2], exit_code=int(sys.argv[3]), log_tail=sys.argv[4])
" "$JOB" "任務執行失敗 (exit=$rc)" "$rc" "$TAIL" || true
fi

exit "$rc"
