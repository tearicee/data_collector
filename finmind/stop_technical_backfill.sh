#!/bin/bash
# ============================================================
# 停止 FinMind「技術面」歷史回溯下載 (手動使用)
# 只停技術面 backfill，不影響：
#   - 每日股票 tick 更新 daily_update.sh / 技術面每日更新 daily_technical.sh
#   - 期貨/選擇權/分點 backfill、期交所等其他排程
# 下載器跳過已下載檔，重新執行 start_technical_backfill.sh 即從中斷處續傳。
# 注意：分K 與分點都用 download_tick.py，故以完整參數字串精準比對，避免誤殺。
# ============================================================
DIR=/home/tearicee/data_collector/finmind
CTL="$DIR/backfill_control.log"
ts(){ date '+%Y-%m-%d %H:%M:%S'; }

if pkill -f "download_technical.py"; then
    echo "$(ts) [STOP] 已停止 技術面 /data 回補" >> "$CTL"
fi
if pkill -f "download_tick.py --dataset TaiwanStockKBar"; then
    echo "$(ts) [STOP] 已停止 分K 回補" >> "$CTL"
fi
echo "$(ts) [STOP] 技術面回補暫停完成" >> "$CTL"
