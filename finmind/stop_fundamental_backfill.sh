#!/bin/bash
# 停止 FinMind 基本面回溯下載
CTL=/home/tearicee/data_collector/finmind/backfill_control.log
ts(){ date '+%Y-%m-%d %H:%M:%S'; }
pids=$(pgrep -f "download_fundamental.py")
if [ -z "$pids" ]; then
    echo "沒有執行中的基本面回補程序"
    exit 0
fi
echo "停止基本面回補 pid: $pids"
kill $pids
echo "$(ts) [STOP] 基本面 /data 回補 pid=$pids" >> "$CTL"
