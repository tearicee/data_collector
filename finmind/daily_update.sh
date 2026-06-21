#!/bin/bash
# ============================================================
# 每日自動更新「全部」FinMind 資料集 (直到 API token 失效為止)
#   storage_objects: TaiwanStockPriceTick, TaiwanStockTradingDailyReport
#   /api/v4/data    : TaiwanFuturesTick, TaiwanOptionTick
# 流程：
#   1. 檢查 token 是否仍為有效 SponsorPro，過期/降級則自動停止 (不打 API)
#   2. 下載最近 7 天視窗 (自動跳過已下載/假日，補抓延遲發布的資料)
#   3. 只把股票 tick 同步到 Google Drive (其餘依設定僅存本機 D 槽)
# 各下載器內建防封鎖：連續失敗熔斷 + 每請求最小間隔 + 401/402/403 立即中止
# ============================================================

PROJECT_DIR=/home/tearicee/data_collector/finmind
VENV_PY=/home/tearicee/data_collector/.venv/bin/python   # 共用 data_collector/.venv
LOG_DIR=/mnt/d/finmind_data/logs
LOG_FILE="$LOG_DIR/daily_update.log"

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR" || exit 1

TS() { date '+%Y-%m-%d %H:%M:%S'; }

# --- 1. 檢查 token 是否仍有效 (失效即停止，達成「直到 API key 失效為止」) ---
CHECK=$("$VENV_PY" check_token.py 2>>"$LOG_FILE")
if [ "$CHECK" != "OK" ]; then
    echo "$(TS) [STOP] token 已失效或降級，停止下載: $CHECK" >> "$LOG_FILE"
    echo "$(TS) [STOP] 如已續訂 SponsorPro，下次排程會自動恢復；否則可移除此 cron。" >> "$LOG_FILE"
    exit 0
fi

# --- 2. 下載最近 7 天 (含延遲發布補抓) ---
START=$(date -d '7 days ago' +%F)
END=$(date +%F)
echo "$(TS) [INFO] token 有效，更新全部資料集，視窗 $START ~ $END" >> "$LOG_FILE"

# storage_objects 整日資料 (parquet 直出)
"$VENV_PY" download_tick.py --dataset TaiwanStockPriceTick \
    --start "$START" --end "$END" --reverse >> "$LOG_FILE" 2>&1
"$VENV_PY" download_tick.py --dataset TaiwanStockTradingDailyReport \
    --start "$START" --end "$END" --reverse >> "$LOG_FILE" 2>&1

# /api/v4/data 期貨 + 選擇權逐筆 (JSON→parquet)
"$VENV_PY" download_data_tick.py --datasets TaiwanFuturesTick,TaiwanOptionTick \
    --start "$START" --end "$END" --reverse >> "$LOG_FILE" 2>&1

# --- 3. 只同步股票 tick 到 Google Drive (期貨/選擇權/分點依設定不上雲) ---
echo "$(TS) [INFO] 下載完成，同步股票 tick 到 Google Drive" >> "$LOG_FILE"
"$PROJECT_DIR/backup_to_gdrive.sh"
echo "$(TS) [INFO] 每日更新流程結束" >> "$LOG_FILE"
