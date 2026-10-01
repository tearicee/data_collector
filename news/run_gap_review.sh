#!/bin/bash
# 開盤跳空檢討：09:05 週一~五 (非交易日由程式自行判斷略過)。shioaji 在 daily_script/.venv-intraday
#   5 9 * * 1-5 /home/tearicee/data_collector/news/run_gap_review.sh
DC_ROOT=/home/tearicee/data_collector
PY=/home/tearicee/daily_script/.venv-intraday/bin/python
mkdir -p /mnt/d/mops/news/logs
cd "$DC_ROOT" && "$DC_ROOT/common/run_with_alert.sh" open_gap_review -- "$PY" news/open_gap_review.py >> /mnt/d/mops/news/logs/gap_review.log 2>&1
