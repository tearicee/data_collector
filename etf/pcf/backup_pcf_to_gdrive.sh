#!/bin/bash
# ============================================================
# 備份 ETF 申購買回清單資料集 (D:\etf_pcf) 到 Google Drive
# 使用 rclone copy 只上傳新增/變更檔案，不刪除雲端多餘檔案
# 失敗時以 rclone 的結束碼結束，讓 run_with_alert.sh 發告警
# ============================================================

SRC="/mnt/d/etf_pcf/"
DST="gdrive:etf_pcf/"
LOG_DIR="/mnt/d/etf_pcf/logs"
LOG_FILE="$LOG_DIR/backup.log"

mkdir -p "$LOG_DIR"

echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] 開始備份申購買回清單資料集到 Google Drive..." >> "$LOG_FILE"

rclone copy "$SRC" "$DST" \
    --exclude "logs/**" \
    --exclude "*.tmp" \
    --transfers 4 \
    --log-file "$LOG_FILE" \
    --log-level INFO

rc=$?
if [ $rc -eq 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] 備份完成" >> "$LOG_FILE"
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') [ERROR] 備份失敗 (exit code: $rc)" >> "$LOG_FILE"
fi
exit $rc
