#!/bin/bash
# ============================================================
# 排程 log 輪替：超過上限的 *.log / *.jsonl 壓縮封存成 <檔名>.<YYYYMMDD>.gz，原檔清空續寫
#   (copytruncate 做法：正在 >> 追加寫入的程式不受影響)
#   cron 建議每月 1 日 06:30；也可手動 ./rotate_logs.sh [MB 上限，預設 5]
#   tick_alert 的 log 自己有按日輪替，不在這裡處理
# ============================================================
LIMIT_MB="${1:-5}"
DIRS=(
    /home/tearicee/data_collector/finmind
    /home/tearicee/data_collector/taifex
    /home/tearicee/data_collector/common
    /home/tearicee/daily_script/logs
    /mnt/d/monitoring
    /mnt/d/mops/news/logs
    /mnt/d/finmind_data/logs
    /mnt/d/etf_daily_holdings/data/logs
    /mnt/d/etf_daily_holdings_raw/logs
    /mnt/d/etf_pcf/logs
    /mnt/d/taiwanindex/technical_notice/logs
)
stamp=$(date +%Y%m%d)
n=0
for d in "${DIRS[@]}"; do
    [ -d "$d" ] || continue
    while IFS= read -r -d '' f; do
        gz="${f%.*}.${stamp}.${f##*.}.gz"
        if gzip -c "$f" > "$gz"; then
            : > "$f"
            echo "$(date '+%F %T') 封存 $f -> $(basename "$gz") ($(du -h "$gz" | cut -f1))"
            n=$((n + 1))
        else
            echo "$(date '+%F %T') 壓縮失敗，保留原檔: $f" >&2
            rm -f "$gz"
        fi
    done < <(find "$d" -maxdepth 1 -type f \( -name '*.log' -o -name '*.jsonl' \) -size +"${LIMIT_MB}"M -print0)
done
echo "$(date '+%F %T') 完成，封存 $n 個檔案 (上限 ${LIMIT_MB} MB)"
