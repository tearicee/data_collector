#!/bin/bash
# ============================================================
# 備份投信原始持股資料到 Google Drive
# 使用 rclone copy 只上傳新增/變更檔案，不刪除雲端多餘檔案
# ============================================================

SRC="/mnt/d/etf_daily_holdings_raw/"
DST="gdrive:etf_daily_holdings_raw/"
LOG_DIR="/mnt/d/etf_daily_holdings_raw/logs"
LOG_FILE="$LOG_DIR/backup.log"

mkdir -p "$LOG_DIR"

echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] 開始備份投信原始持股到 Google Drive..." >> "$LOG_FILE"

rclone copy "$SRC" "$DST" \
    --exclude "logs/**" \
    --transfers 4 \
    --log-file "$LOG_FILE" \
    --log-level INFO

rc=$?
if [ $rc -eq 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] 備份完成" >> "$LOG_FILE"
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') [ERROR] 備份失敗 (exit code: $rc)" >> "$LOG_FILE"
fi
