#!/bin/bash
# ============================================================
# 備份 ETF 資料到 Google Drive
# 使用 rclone copy 只上傳新增/變更檔案，不刪除雲端多餘檔案
#   1. CMoney 持股      data/        → gdrive:etf_daily_holdings/
#   2. MOPS 基金主檔    fund_master/ → gdrive:etf_daily_holdings/fund_master/
# ============================================================

LOG_DIR="/mnt/d/etf_daily_holdings/data/logs"
LOG_FILE="$LOG_DIR/backup.log"
mkdir -p "$LOG_DIR"

rc_all=0

echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] 開始備份 CMoney 持股..." >> "$LOG_FILE"
rclone copy "/mnt/d/etf_daily_holdings/data/" "gdrive:etf_daily_holdings/" \
    --exclude "logs/**" \
    --transfers 4 \
    --log-file "$LOG_FILE" \
    --log-level INFO
rc=$?; [ $rc -ne 0 ] && rc_all=$rc

echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] 開始備份 MOPS 基金主檔..." >> "$LOG_FILE"
rclone copy "/mnt/d/etf_daily_holdings/fund_master/" "gdrive:etf_daily_holdings/fund_master/" \
    --transfers 4 \
    --log-file "$LOG_FILE" \
    --log-level INFO
rc=$?; [ $rc -ne 0 ] && rc_all=$rc

if [ $rc_all -eq 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] 備份完成" >> "$LOG_FILE"
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') [ERROR] 備份失敗 (exit code: $rc_all)" >> "$LOG_FILE"
fi
exit $rc_all
