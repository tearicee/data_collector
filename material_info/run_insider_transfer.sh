#!/bin/bash
# 內部人申報轉讓 (證交所/櫃買開放資料快照) - cron：5 9,12,17,21 * * 1-5
DC_ROOT=/home/tearicee/data_collector
LOG_DIR=/mnt/d/mops/insider_transfer/logs
mkdir -p "$LOG_DIR"
cd "$DC_ROOT" || exit 1
"$DC_ROOT/common/run_with_alert.sh" insider_transfer -- "$DC_ROOT/.venv/bin/python" "$DC_ROOT/material_info/download_insider_transfer.py" \
    >> "$LOG_DIR/poll_$(date +%Y%m).log" 2>&1
