#!/bin/bash
# ============================================================
# ETF 資料 pipeline - cron 啟動腳本 (每天 18:55 觸發)
# 流程 (各步驟經 run_with_alert.sh 包裝：寫心跳 + 失敗即時 Discord 告警):
#   1. mops_fund_list.py  更新 MOPS 基金主檔 + 偵測新代號 (先更新名單)
#   2. etf_crawler.py      CMoney 全母體持股 (讀主檔名單，內建 0~10 分隨機延遲)
#   3. raw_holdings/run_raw.py  投信官網原始持股 (已實作 adapter 者)
#   4. 備份到 Google Drive (CMoney+主檔、投信原始各自 rclone)
# 某步失敗不中斷後續 (例如 MOPS 失敗仍讓爬蟲用既有名單續跑)。
# ============================================================

PROJECT_DIR=/home/tearicee/data_collector/etf
COMMON=/home/tearicee/data_collector/common
VENV_PYTHON=/home/tearicee/data_collector/.venv/bin/python   # 共用 data_collector/.venv
LOG_DIR=/mnt/d/etf_daily_holdings/data/logs
CRON_LOG="$LOG_DIR/cron.log"

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR"

# 1. MOPS 基金主檔 (先更新名單，供步驟 2 讀取)
"$COMMON/run_with_alert.sh" mops_fund_list -- \
    "$VENV_PYTHON" mops_fund_list.py --now >> "$CRON_LOG" 2>&1

# 2. CMoney 全母體持股 (etf_crawler 內建隨機延遲)
"$COMMON/run_with_alert.sh" etf_crawler -- \
    "$VENV_PYTHON" etf_crawler.py >> "$CRON_LOG" 2>&1

# 3. 投信官網原始持股
"$COMMON/run_with_alert.sh" etf_raw -- \
    "$VENV_PYTHON" raw_holdings/run_raw.py --now >> "$CRON_LOG" 2>&1

# 4. 備份到 Google Drive (經 wrapper 以便備份失敗也告警)
"$COMMON/run_with_alert.sh" etf_backup -- \
    "$PROJECT_DIR/backup_to_gdrive.sh" >> "$CRON_LOG" 2>&1
"$COMMON/run_with_alert.sh" etf_raw_backup -- \
    "$PROJECT_DIR/raw_holdings/backup_raw_to_gdrive.sh" >> "$CRON_LOG" 2>&1
