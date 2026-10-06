#!/bin/bash
# ============================================================
# 每月營收彙總表輪詢 (MOPS 舊站靜態頁，上市/上櫃/興櫃各 1 請求) - cron 啟動腳本
#   新公告的營收 → /mnt/d/mops/month_revenue/月營收_YYYY-MM.parquet，
#   新鮮度流程 (run_pipeline.sh) 會把它當事件評分 (source=月營收)。
# cron：15,45 8-22 * * *   /home/tearicee/data_collector/material_info/run_month_revenue.sh
# ============================================================
DC_ROOT=/home/tearicee/data_collector
LOG_DIR=/mnt/d/mops/month_revenue/logs
mkdir -p "$LOG_DIR"
cd "$DC_ROOT" || exit 1
{
    exec 9>/mnt/d/mops/month_revenue/.lock
    flock -n 9 || { echo "$(date '+%F %T') [SKIP] 上一輪尚未結束"; exit 0; }
    "$DC_ROOT/common/run_with_alert.sh" month_revenue -- \
        "$DC_ROOT/.venv/bin/python" "$DC_ROOT/material_info/download_month_revenue.py"
} >> "$LOG_DIR/poll_$(date +%Y%m).log" 2>&1
