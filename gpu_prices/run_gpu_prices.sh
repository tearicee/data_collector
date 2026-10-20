#!/bin/bash
# GPU 雲價格每週收集 (cron 建議週一 09:30)；經 run_with_alert 寫心跳 gpu_prices、失敗才告警
DC_ROOT=/home/tearicee/data_collector
LOG_DIR=/mnt/d/gpu_cloud_prices/logs
mkdir -p "$LOG_DIR"
cd "$DC_ROOT" || exit 1
"$DC_ROOT/common/run_with_alert.sh" --cadence weekly gpu_prices -- \
    "$DC_ROOT/.venv/bin/python" gpu_prices/collect.py --compare >> "$LOG_DIR/collect.log" 2>&1
