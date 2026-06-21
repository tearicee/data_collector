#!/bin/bash
# ============================================================
# 備份 TaiwanStockPriceTick 逐筆資料到 Google Drive
# 使用 rclone copy 只上傳新增/變更檔案，不刪除雲端多餘檔案
# 比照 etf_daily_holdings/backup_to_gdrive.sh 的做法
# ============================================================

SRC="/mnt/d/finmind_data/TaiwanStockPriceTick/"
DST="gdrive:finmind_data/TaiwanStockPriceTick/"
LOG_DIR="/mnt/d/finmind_data/logs"
LOG_FILE="$LOG_DIR/backup.log"

mkdir -p "$LOG_DIR"

echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] 開始備份 TaiwanStockPriceTick 到 Google Drive..." >> "$LOG_FILE"

rclone copy "$SRC" "$DST" \
    --transfers 4 \
    --log-file "$LOG_FILE" \
    --log-level INFO

if [ $? -eq 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] 備份完成" >> "$LOG_FILE"
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') [ERROR] 備份失敗 (exit code: $?)" >> "$LOG_FILE"
fi
