#!/bin/bash
# ============================================================
# ETF 資料 pipeline - cron 啟動腳本 (每天 18:55 觸發)
# 流程 (各步驟經 run_with_alert.sh 包裝：寫心跳 + 失敗即時 Discord 告警):
#   1. mops_fund_list.py            更新 MOPS 基金主檔 + 偵測新代號 (先更新名單)
#   2. raw_holdings/run_raw.py      投信官網原始持股 (已實作 adapter 者)
#   3. raw_holdings/to_cmoney.py    轉成 CMoney 格式寫到 etf_daily_holdings/data (下游讀這裡)
#   4. 等到 SECOND_PASS_AT 再跑一輪 2+3，補上較晚公告的投信
#   5. 備份到 Google Drive (CMoney 格式+主檔、投信原始各自 rclone)
# 某步失敗不中斷後續 (例如 MOPS 失敗仍讓爬蟲用既有名單續跑)。
#
# 2026-10 起不再跑 etf_crawler.py: CMoney 端點改成要驗證 (回 Auth Failed)，
# 234 檔全數失敗且每晚重試 7 小時。持股改由步驟 2+3 提供，格式與日期語意與 CMoney 時期相同
# (見 raw_holdings/to_cmoney.py 開頭說明)。
#
# 兩輪的時間: 第一輪趕在 daily_script 19:35 的 ETF 異動通知之前；
# 第二輪沿用過去投信爬蟲實際執行的時段 (CMoney 兩輪跑完後約 20:40~21:00)。
# 手動執行不想等第二輪: SKIP_SECOND_PASS=1 etf/run_crawler.sh
# ============================================================

PROJECT_DIR=/home/tearicee/data_collector/etf
COMMON=/home/tearicee/data_collector/common
VENV_PYTHON=/home/tearicee/data_collector/.venv/bin/python   # 共用 data_collector/.venv
LOG_DIR=/mnt/d/etf_daily_holdings/data/logs
CRON_LOG="$LOG_DIR/cron.log"
SECOND_PASS_AT="${SECOND_PASS_AT:-20:40}"

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR"

# 投信原始持股 + 轉檔；$@ 傳給 to_cmoney.py
fetch_and_convert() {
    "$COMMON/run_with_alert.sh" etf_raw -- \
        "$VENV_PYTHON" raw_holdings/run_raw.py --now >> "$CRON_LOG" 2>&1
    "$COMMON/run_with_alert.sh" etf_holdings_convert -- \
        "$VENV_PYTHON" raw_holdings/to_cmoney.py --quiet "$@" >> "$CRON_LOG" 2>&1
}

# 1. MOPS 基金主檔 (先更新名單，供步驟 2 讀取)
"$COMMON/run_with_alert.sh" mops_fund_list -- \
    "$VENV_PYTHON" mops_fund_list.py --now >> "$CRON_LOG" 2>&1

# 2~4. 投信原始持股 → CMoney 格式，兩輪；最後一輪才檢查最新交易日的檔數
if [ -n "${SKIP_SECOND_PASS:-}" ]; then
    fetch_and_convert --require-latest
else
    fetch_and_convert
    wait_s=$(( $(date -d "today $SECOND_PASS_AT" +%s) - $(date +%s) ))
    if [ "$wait_s" -gt 0 ]; then
        echo "$(date '+%F %T') 等到 $SECOND_PASS_AT 跑第二輪 (${wait_s}s)" >> "$CRON_LOG"
        sleep "$wait_s"
    fi
    fetch_and_convert --require-latest
fi

# 5. 備份到 Google Drive (經 wrapper 以便備份失敗也告警)
"$COMMON/run_with_alert.sh" etf_backup -- \
    "$PROJECT_DIR/backup_to_gdrive.sh" >> "$CRON_LOG" 2>&1
"$COMMON/run_with_alert.sh" etf_raw_backup -- \
    "$PROJECT_DIR/raw_holdings/backup_raw_to_gdrive.sh" >> "$CRON_LOG" 2>&1
