#!/bin/bash
# ============================================================
# 每日健檢 - cron 啟動腳本 (建議排 07:00，在所有夜間任務之後)
# ============================================================
DC_ROOT=/home/tearicee/data_collector
VENV_PY="$DC_ROOT/.venv/bin/python"

cd "$DC_ROOT"
"$VENV_PY" common/daily_healthcheck.py
