#!/bin/bash
# ============================================================
# 台灣指數公司技術通知下載器 - cron 啟動腳本
# 每天下載當天發布的「成分股審核結果」與「指數定期審核日程表」
# 解析後寫入 D 槽 CSV，並備份到 Google Drive
# cron（每晚 19:30）：
#   30 19 * * * /home/tearicee/data_collector/TIP/run_tip.sh
# ============================================================

PROJECT_DIR=/home/tearicee/data_collector/TIP
VENV_PYTHON=/home/tearicee/data_collector/.venv/bin/python   # 共用 data_collector/.venv
LOG_DIR=/mnt/d/taiwanindex/technical_notice/logs

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR"

# --days 2 容錯：補抓昨天可能盤後較晚才發布、昨日 cron 未抓到的報告（已處理者自動去重跳過）
"$VENV_PYTHON" download_notice.py --days 2 >> "$LOG_DIR/cron.log" 2>&1

# 依本機已下載 PDF 重建「追蹤 ETF 成分股異動全史」CSV（含更名鏈 + xlsx 成立初始成分股）
"$VENV_PYTHON" rebuild_tracked_history.py >> "$LOG_DIR/cron.log" 2>&1

# 下載完成後備份到 Google Drive
"$PROJECT_DIR/backup_to_gdrive.sh"
