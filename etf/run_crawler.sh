#!/bin/bash
# ============================================================
# ETF 持股爬蟲 - cron 啟動腳本
# cron 於每天 18:55 觸發，Python 腳本內建 0~10 分鐘隨機延遲
# 實際爬蟲時間落在 18:55~19:05
# ============================================================

PROJECT_DIR=/home/tearicee/data_collector/etf
VENV_PYTHON=/home/tearicee/data_collector/.venv/bin/python   # 共用 data_collector/.venv
LOG_DIR=/mnt/d/etf_daily_holdings/data/logs

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR"
"$VENV_PYTHON" etf_crawler.py >> "$LOG_DIR/cron.log" 2>&1

# 爬蟲完成後備份到 Google Drive
"$PROJECT_DIR/backup_to_gdrive.sh"
