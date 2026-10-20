#!/bin/bash
# GPU 雲價格每日收集 (cron 09:30)；經 run_with_alert 寫心跳 gpu_prices、失敗才發錯誤回報頻道
# 官網揭露的是牌價，變動是離散事件 (幾週一次)；每天 4 次請求成本極低，換到確切的調價日期
DC_ROOT=/home/tearicee/data_collector
LOG_DIR=/mnt/d/gpu_cloud_prices/logs
mkdir -p "$LOG_DIR"
cd "$DC_ROOT" || exit 1
"$DC_ROOT/common/run_with_alert.sh" --cadence daily gpu_prices -- \
    "$DC_ROOT/.venv/bin/python" gpu_prices/collect.py --compare >> "$LOG_DIR/collect.log" 2>&1
