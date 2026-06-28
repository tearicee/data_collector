#!/bin/bash
# ============================================================
# SPONSOR 降級後 — bulk 失效資料集的「逐單位」每日下載 wrapper
#   用法: daily_sponsor_fallback.sh <dataset>
#     TaiwanStockPriceTick / TaiwanStockKBar              → download_tick_by_stock.py (逐股票)
#     TaiwanStockTradingDailyReport / ...WarrantTradingDailyReport → download_daily_report_by_broker.py (逐券商)
#
#   降級前(SponsorPro)請勿啟用此排程：bulk 1 請求/日 既便宜又完整，仍應用 download_tick.py。
#   降級後(Sponsor)才把對應 cron 從 bulk 換成本 wrapper。詳見 SPONSOR_DOWNGRADE_PLAN.md。
#
#   近 N 天視窗 skip-existing：穩態下只抓新交易日；補延遲/漏抓。
#   含 token 檢查、pgrep 防重複、blackout 08:00-14:30、原子寫檔(下載器內建)。
# ============================================================
DS="$1"
WINDOW_DAYS="${2:-7}"
DIR=/home/tearicee/data_collector/finmind
PY=/home/tearicee/data_collector/.venv/bin/python
LOG_DIR=/mnt/d/finmind_data/logs
BLACKOUT="08:00-14:30"
ts(){ date '+%Y-%m-%d %H:%M:%S'; }
mkdir -p "$LOG_DIR"
cd "$DIR" || exit 1

case "$DS" in
  TaiwanStockPriceTick|TaiwanStockKBar)
    SCRIPT="download_tick_by_stock.py" ;;
  TaiwanStockTradingDailyReport|TaiwanStockWarrantTradingDailyReport)
    SCRIPT="download_daily_report_by_broker.py" ;;
  *)
    echo "$(ts) [ERR] 未知 dataset: $DS" ; exit 1 ;;
esac
LOG_FILE="$LOG_DIR/daily_sponsor_${DS}.log"

# token 檢查
CHECK=$("$PY" check_token.py 2>>"$LOG_FILE")
if [ "$CHECK" != "OK" ]; then
    echo "$(ts) [STOP] token 失效或降級失敗: $CHECK" >> "$LOG_FILE"; exit 0
fi
# 防重複 (同 dataset)
if pgrep -f "$SCRIPT --dataset $DS" >/dev/null; then
    echo "$(ts) [SKIP] $DS 逐單位下載已在執行" >> "$LOG_FILE"; exit 0
fi

START=$(date -d "$WINDOW_DAYS days ago" +%F)
END=$(date +%F)
echo "$(ts) [START] $DS 逐單位下載 視窗 $START~$END ($SCRIPT)" >> "$LOG_FILE"
"$PY" "$DIR/$SCRIPT" --dataset "$DS" --start "$START" --end "$END" --reverse \
    --blackout "$BLACKOUT" >> "$LOG_FILE" 2>&1
echo "$(ts) [DONE] $DS rc=$?" >> "$LOG_FILE"
