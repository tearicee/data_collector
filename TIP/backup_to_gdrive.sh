#!/bin/bash
# ============================================================
# 備份台灣指數公司技術通知資料到 Google Drive
# 使用 rclone copy 只上傳新增/變更檔案，不刪除雲端多餘檔案
# ============================================================

SRC="/mnt/d/taiwanindex/technical_notice/"
DST="gdrive:taiwanindex/technical_notice/"
LOG_DIR="/mnt/d/taiwanindex/technical_notice/logs"
LOG_FILE="$LOG_DIR/backup.log"

mkdir -p "$LOG_DIR"

echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] 開始備份到 Google Drive..." >> "$LOG_FILE"

rclone copy "$SRC" "$DST" \
    --exclude "logs/**" \
    --log-file "$LOG_FILE" \
    --log-level INFO

if [ $? -eq 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] 備份完成" >> "$LOG_FILE"
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') [ERROR] 備份失敗 (exit code: $?)" >> "$LOG_FILE"
fi
