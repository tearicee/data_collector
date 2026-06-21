#!/bin/bash
# ============================================================
# 期交所每日資料 - cron 啟動腳本
# 1. 下載前一交易日的期貨 + 選擇權資料 (download_taifex_daily.py)
# 2. 同步到 Google Drive 的 Taifex 資料夾
#
# 註：Python 腳本內建 FileHandler 會自行寫入 taifex_download.log，
#     因此這裡把 stdout 導向 /dev/null 避免日誌重複，只把 stderr
#     (未捕捉的例外 traceback) 附加進同一個 log 以利除錯。
# ============================================================

SCRIPT_DIR=/home/tearicee/data_collector/taifex
VENV_PY=/home/tearicee/data_collector/.venv/bin/python   # 共用 data_collector/.venv

"$VENV_PY" "$SCRIPT_DIR/download_taifex_daily.py" \
    > /dev/null 2>> "$SCRIPT_DIR/taifex_download.log"

"$SCRIPT_DIR/backup_taifex_to_gdrive.sh"
