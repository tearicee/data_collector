#!/bin/bash
# ============================================================
# 啟動 FinMind「台股 - 技術面」歷史回溯下載 (detached，完整儲存)
#   1) /data 技術面全部資料集 (download_technical.py)，各自從已知最早日回補：
#        TaiwanStockPrice / TaiwanStockPriceAdj      2000-01-01 起
#        TaiwanStockWeekPrice 2000-01-03 / TaiwanStockMonthPrice 2000-01-01
#        TaiwanStockPriceLimit / *5Seconds / Every5SecondsIndex 2010 起
#        TaiwanStockDayTrading 2015 起；快照表即時整表抓回
#   2) 分K TaiwanStockKBar (storage_objects)，2019-01-02 起
# 兩支都跳過已下載檔 → 可安全重複執行 (續傳)。
# 內建 --blackout 08:00-13:31：盤中自動暫停 (不發網路請求)，盤後自動續跑，
#   故無需 stop/start cron；想手動停止用 stop_technical_backfill.sh。
# 已在執行則跳過，避免重複。
# ============================================================
DIR=/home/tearicee/data_collector/finmind
PY=/home/tearicee/data_collector/.venv/bin/python   # 共用 data_collector/.venv
TODAY=$(date +%F)
BLACKOUT="08:00-13:31"
CTL="$DIR/backfill_control.log"
ts(){ date '+%Y-%m-%d %H:%M:%S'; }

# --- /data 技術面全集 ---
if pgrep -f "download_technical.py" >/dev/null; then
    echo "$(ts) [SKIP] 技術面 /data 回補 已在執行" >> "$CTL"
else
    nohup "$PY" "$DIR/download_technical.py" --end "$TODAY" --reverse \
        --blackout "$BLACKOUT" \
        > /dev/null 2>> "$DIR/download_technical.log" &
    echo "$(ts) [START] 技術面 /data 回補 pid=$!" >> "$CTL"
fi

# --- 分K storage_objects ---
if pgrep -f "download_tick.py --dataset TaiwanStockKBar" >/dev/null; then
    echo "$(ts) [SKIP] 分K 回補 已在執行" >> "$CTL"
else
    nohup "$PY" "$DIR/download_tick.py" --dataset TaiwanStockKBar \
        --start 2019-01-02 --end "$TODAY" --reverse --blackout "$BLACKOUT" \
        > /dev/null 2>> "$DIR/download_tick.log" &
    echo "$(ts) [START] 分K 回補 pid=$!" >> "$CTL"
fi
