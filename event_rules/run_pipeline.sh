#!/bin/bash
# ============================================================
# 重訊 + 新聞 新鮮度每日流程 (純規則，不經 AI 模型)
#   1. 重訊過濾標記      material_info/filter_material_info.py  → derived/重訊篩選_*.parquet
#   2. 重訊標籤+數據抽取  material_info/extract_events.py        → derived/重訊事件_*.parquet
#   3. 新聞標籤 (近兩月)  news/tag_news.py                       → news/tagged/新聞標籤_*.parquet
#   4. 新鮮度評分        event_rules/freshness.py               → news/scored/新鮮度_*.parquet
#                                                                + news/review/新鮮度檢視_YYYY-MM-DD.csv
# cron：40 7,13,18,22 * * *  (早盤前、午盤、收盤後、晚間重訊高峰後)
# ============================================================
DC_ROOT=/home/tearicee/data_collector
PY="$DC_ROOT/.venv/bin/python"
LOG_DIR=/mnt/d/mops/news/logs
mkdir -p "$LOG_DIR" /mnt/d/mops/news/state
cd "$DC_ROOT" || exit 1
THIS=$(date +%Y-%m); PREV=$(date -d "$(date +%Y-%m-01) -1 day" +%Y-%m)

{
    exec 9>/mnt/d/mops/news/state/.pipeline.lock
    flock -n 9 || { echo "$(date '+%F %T') [SKIP] 上一輪尚未結束"; exit 0; }
    echo "==================== $(date '+%F %T') 啟動 ===================="
    "$DC_ROOT/common/run_with_alert.sh" freshness_pipeline -- bash -c "
        set -e
        '$PY' material_info/filter_material_info.py | head -5
        '$PY' material_info/extract_events.py | head -1
        '$PY' news/tag_news.py --months $PREV $THIS
        '$PY' -m event_rules.freshness --review-days 3 --top 10
    "
    rc=$?
    echo "==================== $(date '+%F %T') 結束 (rc=$rc) ===================="
    exit $rc
} >> "$LOG_DIR/pipeline_$(date +%Y%m).log" 2>&1
