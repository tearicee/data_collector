#!/bin/bash
# ============================================================
# 每日更新「台股分點資料表 TaiwanStockTradingDailyReport」(storage_objects bulk)
#   官方更新時間 Mon-Fri 21:00 → 本排程設 22:00 (官方表定後 1 小時緩衝)，避免過早抓不到當日資料。
#   原本併在 daily_update.sh(20:00)，因 20:00 早於官方 21:00 而抓不到當日分點，故獨立出來延後。
#
#   近 7 天視窗 skip-existing：穩態下只抓新交易日、補延遲。含 token 檢查、pgrep 防重複、blackout。
#   (SPONSOR 降級後改用 daily_sponsor_fallback.sh TaiwanStockTradingDailyReport 逐券商版，見 SPONSOR_DOWNGRADE_PLAN.md)
# ============================================================
DIR=/home/tearicee/data_collector/finmind
PY=/home/tearicee/data_collector/.venv/bin/python
LOG_DIR=/mnt/d/finmind_data/logs
LOG_FILE="$LOG_DIR/daily_trading_report.log"
BLACKOUT="08:00-13:31"
ts(){ date '+%Y-%m-%d %H:%M:%S'; }
mkdir -p "$LOG_DIR"
cd "$DIR" || exit 1

CHECK=$("$PY" check_token.py 2>>"$LOG_FILE")
if [ "$CHECK" != "OK" ]; then
    echo "$(ts) [STOP] token 失效或降級: $CHECK" >> "$LOG_FILE"; exit 0
fi
if pgrep -f "download_tick.py --dataset TaiwanStockTradingDailyReport" >/dev/null; then
    echo "$(ts) [SKIP] 分點更新已在執行" >> "$LOG_FILE"; exit 0
fi

START=$(date -d '7 days ago' +%F)
END=$(date +%F)
echo "$(ts) [START] 分點 TradingDailyReport 視窗 $START~$END" >> "$LOG_FILE"
"$PY" "$DIR/download_tick.py" --dataset TaiwanStockTradingDailyReport \
    --start "$START" --end "$END" --reverse --blackout "$BLACKOUT" >> "$LOG_FILE" 2>&1
echo "$(ts) [DONE] 分點 rc=$?" >> "$LOG_FILE"
