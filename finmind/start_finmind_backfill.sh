#!/bin/bash
# ============================================================
# 啟動 FinMind 大型歷史回溯下載 (detached，可被 cron 重啟)
#   1) 期貨+選擇權 TaiwanFuturesTick/TaiwanOptionTick (2019-01-01 ~ 今, /data 端點)
#   2) 分點資料 TaiwanStockTradingDailyReport          (2021-06-30 ~ 今, storage_objects)
# 兩支都會自動跳過已下載檔案 → 可安全重複執行 (續傳)。
# 若該任務已在執行則跳過，避免重複 (供 14:00 重啟 cron 使用)。
# ============================================================
DIR=/home/tearicee/data_collector/finmind
PY=/home/tearicee/data_collector/.venv/bin/python   # 共用 data_collector/.venv
TODAY=$(date +%F)
CTL="$DIR/backfill_control.log"
ts(){ date '+%Y-%m-%d %H:%M:%S'; }

# --- 期貨 (獨立程序，與選擇權並行) ---
if pgrep -f "download_data_tick.py.*TaiwanFuturesTick" >/dev/null; then
    echo "$(ts) [SKIP] 期貨 已在執行" >> "$CTL"
else
    # log() 已自行寫入 download_data_tick.log，stdout 導向 /dev/null 避免重複；stderr 留存 traceback
    nohup "$PY" "$DIR/download_data_tick.py" --datasets TaiwanFuturesTick \
        --start 2019-01-01 --end "$TODAY" --reverse \
        > /dev/null 2>> "$DIR/download_data_tick.log" &
    echo "$(ts) [START] 期貨 pid=$!" >> "$CTL"
fi

# --- 選擇權 (獨立程序，與期貨並行) ---
if pgrep -f "download_data_tick.py.*TaiwanOptionTick" >/dev/null; then
    echo "$(ts) [SKIP] 選擇權 已在執行" >> "$CTL"
else
    nohup "$PY" "$DIR/download_data_tick.py" --datasets TaiwanOptionTick \
        --start 2019-01-01 --end "$TODAY" --reverse \
        > /dev/null 2>> "$DIR/download_data_tick.log" &
    echo "$(ts) [START] 選擇權 pid=$!" >> "$CTL"
fi

# --- 分點資料 ---
if pgrep -f "download_tick.py --dataset TaiwanStockTradingDailyReport" >/dev/null; then
    echo "$(ts) [SKIP] 分點資料 已在執行" >> "$CTL"
else
    # log() 已自行寫入 download_tick.log，stdout 導向 /dev/null 避免重複；stderr 留存 traceback
    nohup "$PY" "$DIR/download_tick.py" --dataset TaiwanStockTradingDailyReport \
        --start 2021-06-30 --end "$TODAY" --reverse \
        > /dev/null 2>> "$DIR/download_tick.log" &
    echo "$(ts) [START] 分點資料 pid=$!" >> "$CTL"
fi
