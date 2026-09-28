#!/bin/bash
# ============================================================
# 全市場「重大訊息」爬蟲 - cron 啟動腳本
#   run_material_info.sh poll   輪詢今天 MOPS 當日重大訊息，只抓新訊息內文
#   run_material_info.sh daily  回掃近 3 天 + 重抓缺內文 + OpenAPI 對帳
#
# 透過 common/run_with_alert.sh 包裝 (失敗 Discord 告警 + 執行層心跳)。
# poll 模式網路失敗由 python 端累計，連續 6 次 (約 1 小時) 才 exit 1 告警，避免洗版。
# flock 防止 poll 與 daily 同時寫同一天的 CSV。
#
# cron：
#   */10 7-23 * * * /home/tearicee/data_collector/material_info/run_material_info.sh poll
#   20 7 * * *      /home/tearicee/data_collector/material_info/run_material_info.sh daily
# ============================================================

DC_ROOT=/home/tearicee/data_collector
PROJECT_DIR="$DC_ROOT/material_info"
VENV_PYTHON="$DC_ROOT/.venv/bin/python"
BASE_DIR=/mnt/d/mops/material_info
LOG_DIR="$BASE_DIR/logs"
LOCK_FILE="$BASE_DIR/state/.lock"

MODE="${1:-poll}"
case "$MODE" in
    poll)  JOB=material_info;       LOG_FILE="$LOG_DIR/poll_$(date +%Y%m).log"; FLOCK_OPT=(-n) ;;
    daily) JOB=material_info_daily; LOG_FILE="$LOG_DIR/daily.log";              FLOCK_OPT=(-w 900) ;;
    *) echo "用法: $0 poll|daily" >&2; exit 2 ;;
esac

mkdir -p "$LOG_DIR" "$(dirname "$LOCK_FILE")"
cd "$PROJECT_DIR" || exit 1

{
    exec 9>"$LOCK_FILE"
    if ! flock "${FLOCK_OPT[@]}" 9; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') [SKIP] 另一個重大訊息任務執行中 ($MODE)"
        exit 0
    fi

    "$DC_ROOT/common/run_with_alert.sh" "$JOB" -- \
        "$VENV_PYTHON" "$PROJECT_DIR/download_material_info.py" --mode "$MODE"
    exit $?
} >> "$LOG_FILE" 2>&1
