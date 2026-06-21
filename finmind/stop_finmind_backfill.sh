#!/bin/bash
# ============================================================
# 停止 FinMind 大型歷史回溯下載 (供 07:30 暫停 cron 使用)
# 只停 backfill，不影響：
#   - 每日股票 tick 更新 daily_update.sh (download_tick.py 無 --dataset)
#   - 期交所 / 其他排程
# 因下載器會跳過已下載檔，14:00 重啟即可從中斷處續傳。
# ============================================================
DIR=/home/tearicee/data_collector/finmind
CTL="$DIR/backfill_control.log"
ts(){ date '+%Y-%m-%d %H:%M:%S'; }

if pkill -f "download_data_tick.py"; then
    echo "$(ts) [STOP] 已停止 期貨/選擇權" >> "$CTL"
fi
if pkill -f "download_tick.py --dataset TaiwanStockTradingDailyReport"; then
    echo "$(ts) [STOP] 已停止 分點資料" >> "$CTL"
fi
echo "$(ts) [STOP] 暫停完成" >> "$CTL"
