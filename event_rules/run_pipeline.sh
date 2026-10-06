#!/bin/bash
# ============================================================
# 重訊 + 新聞 新鮮度每日流程 (純規則，不經 AI 模型)
#   1. 重訊過濾標記      material_info/filter_material_info.py  → derived/重訊篩選_*.parquet
#   2. 重訊標籤+數據抽取  material_info/extract_events.py        → derived/重訊事件_*.parquet
#   3. 新聞標籤 (有變動的月份) news/tag_news.py --changed                      → news/tagged/新聞標籤_*.parquet
#   4. 新鮮度評分        event_rules/freshness.py               → news/scored/新鮮度_*.parquet
#                                                                + news/review/新鮮度檢視_YYYY-MM-DD.csv
#   5. Discord 即時推播  event_rules/push_discord.py            → 事件分 ≥7 且未推過者
# cron：*/20 7-23 * * *；另 14:05 (週一~五) push_discord.py --mode daily 當日總結
# ============================================================
DC_ROOT=/home/tearicee/data_collector
PY="$DC_ROOT/.venv/bin/python"
LOG_DIR=/mnt/d/mops/news/logs
mkdir -p "$LOG_DIR" /mnt/d/mops/news/state
cd "$DC_ROOT" || exit 1

{
    exec 9>/mnt/d/mops/news/state/.pipeline.lock
    flock -n 9 || { echo "$(date '+%F %T') [SKIP] 上一輪尚未結束"; exit 0; }
    echo "==================== $(date '+%F %T') 啟動 ===================="
    "$DC_ROOT/common/run_with_alert.sh" freshness_pipeline -- bash -c "
        set -e
        '$PY' material_info/filter_material_info.py --recent 2 | head -5
        '$PY' material_info/extract_events.py --recent 2 | head -1
        '$PY' news/tag_news.py --changed
        '$PY' -m event_rules.freshness --review-days 3 --top 10
        '$PY' event_rules/push_discord.py --mode instant | tail -1
    "
    rc=$?
    echo "==================== $(date '+%F %T') 結束 (rc=$rc) ===================="
    exit $rc
} >> "$LOG_DIR/pipeline_$(date +%Y%m).log" 2>&1
