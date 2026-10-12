#!/bin/bash
# 法說會逐字稿/Memo 每日抓取 (AlphaMemo 免費頁，最低限度：1 次清單 + 只抓沒抓過的、每頁間隔 8~12 秒)
# cron：5 22 * * *   /home/tearicee/data_collector/news/run_earnings_calls.sh
DC_ROOT=/home/tearicee/data_collector
LOG_DIR=/mnt/d/mops/news/earnings_call/logs
mkdir -p "$LOG_DIR"
cd "$DC_ROOT" || exit 1
"$DC_ROOT/common/run_with_alert.sh" earnings_call -- "$DC_ROOT/.venv/bin/python" "$DC_ROOT/news/collect_earnings_calls.py" \
    >> "$LOG_DIR/collect_$(date +%Y%m).log" 2>&1
