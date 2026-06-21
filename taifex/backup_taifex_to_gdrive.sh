#!/bin/bash
# ============================================================
# 備份期交所每日資料到 Google Drive 的 Taifex 資料夾
# 使用 rclone copy 只上傳新增/變更檔案，不刪除雲端多餘檔案
# 本機與雲端結構一致：
#   /mnt/d/Taifex/futures/ (台指期/期貨) -> gdrive:Taifex/futures/
#   /mnt/d/Taifex/options/ (選擇權)      -> gdrive:Taifex/options/
# ============================================================

SRC="/mnt/d/Taifex/"
DST="gdrive:Taifex/"
LOG_FILE="/home/tearicee/data_collector/taifex/taifex_backup.log"

echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] 開始備份期交所資料到 Google Drive..." >> "$LOG_FILE"

rclone copy "$SRC" "$DST" \
    --exclude "logs/**" \
    --transfers 4 \
    --log-file "$LOG_FILE" \
    --log-level INFO

if [ $? -eq 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] 備份完成 (futures + options)" >> "$LOG_FILE"
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') [ERROR] 備份失敗 (exit code: $?)" >> "$LOG_FILE"
fi
