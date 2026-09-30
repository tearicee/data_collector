#!/bin/bash
# 財經新聞輪詢 cron 啟動腳本 (鉅亨 API + 經濟日報/科技新報 RSS → D:/mops/news/data)
#   */15 7-23 * * * /home/tearicee/data_collector/news/run_news.sh
DC_ROOT=/home/tearicee/data_collector
LOG_DIR=/mnt/d/mops/news/logs
mkdir -p "$LOG_DIR" /mnt/d/mops/news/state
{
    exec 9>/mnt/d/mops/news/state/.poll.lock
    flock -n 9 || { echo "$(date '+%F %T') [SKIP] 上一輪尚未結束"; exit 0; }
    "$DC_ROOT/common/run_with_alert.sh" news_poll -- \
        "$DC_ROOT/.venv/bin/python" "$DC_ROOT/news/download_news.py" --mode poll --hours 6
} >> "$LOG_DIR/poll_$(date +%Y%m).log" 2>&1
