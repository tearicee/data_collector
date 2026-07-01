#!/bin/bash
# ============================================================
# 每日更新 FinMind「台股 - 技術面」資料集 (直到 token 失效為止)
#   /data 端點 (download_technical.py)：
#     整表快照  : TaiwanStockInfo / TaiwanStockInfoWithWarrant /
#                 TaiwanStockInfoWithWarrantSummary / TaiwanStockTradingDate /
#                 TaiwanStockDayTradingSuspension
#     逐日       : TaiwanStockPrice / TaiwanStockPriceAdj / TaiwanStockDayTrading /
#                 TaiwanStockPriceLimit / TaiwanVariousIndicators5Seconds /
#                 TaiwanStockEvery5SecondsIndex
#     週K / 月K  : TaiwanStockWeekPrice / TaiwanStockMonthPrice
#   storage_objects (download_tick.py)：TaiwanStockKBar
#   (TaiwanStockPriceTick 已由 daily_update.sh 維護，不重複)
#
# 流程：1) 檢查 token 仍為有效 SponsorPro，否則停止  2) 補抓最近視窗
# 各下載器內建防封鎖：連續失敗熔斷 + 每請求最小間隔 + 401/402/403 立即中止。
# 排在夜間 (盤後) 執行，故不會佔用 08:00~14:30 盤中網路；仍帶 --blackout 保險。
# ============================================================
PROJECT_DIR=/home/tearicee/data_collector/finmind
VENV_PY=/home/tearicee/data_collector/.venv/bin/python   # 共用 data_collector/.venv
LOG_DIR=/mnt/d/finmind_data/logs
LOG_FILE="$LOG_DIR/daily_technical.log"
BLACKOUT="08:00-13:31"

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR" || exit 1
TS() { date '+%Y-%m-%d %H:%M:%S'; }

# --- 1. token 檢查 (失效即停止) ---
CHECK=$("$VENV_PY" check_token.py 2>>"$LOG_FILE")
if [ "$CHECK" != "OK" ]; then
    echo "$(TS) [STOP] token 已失效或降級，停止下載: $CHECK" >> "$LOG_FILE"
    exit 0
fi

# --- 2. 最近視窗 (補延遲發布 / 新交易日；月K 需涵蓋整個月初) ---
START=$(date -d '40 days ago' +%F)   # 40 天可確保週一/月初與延遲資料都補到
END=$(date +%F)
echo "$(TS) [INFO] token 有效，更新技術面資料集，視窗 $START ~ $END" >> "$LOG_FILE"

# /data 全部技術面資料集 (快照會整表刷新，逐日/週/月只補缺漏)
"$VENV_PY" download_technical.py --start "$START" --end "$END" --reverse \
    --blackout "$BLACKOUT" >> "$LOG_FILE" 2>&1

# storage_objects 分K
# 【2026-07-01 SPONSOR 降級】bulk 已失效，KBar 改由 daily_sponsor_fallback.sh(逐股票, cron 18:00)維護，此行停用。
# "$VENV_PY" download_tick.py --dataset TaiwanStockKBar \
#     --start "$START" --end "$END" --reverse --blackout "$BLACKOUT" >> "$LOG_FILE" 2>&1

echo "$(TS) [INFO] 技術面每日更新流程結束" >> "$LOG_FILE"
