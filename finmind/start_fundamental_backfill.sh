#!/bin/bash
# ============================================================
# 啟動 FinMind「台股 - 基本面」歷史回溯下載 (detached，完整儲存)
#   單一程序依序回補 12 個基本面資料集 (download_fundamental.py)：
#     綜合損益表/資產負債表/現金流量表 (季)、月營收 (月)、股價市值表 (日)、
#     下市櫃表 (快照)、減資/分割/變更面額參考價 (稀疏整段)、
#     股利政策表/除權除息結果表/市值比重表 (逐股票)
#   跳過已下載檔 → 可安全重複執行 (續傳)。
#   內建 --blackout 08:00-13:31：盤中自動暫停、盤後自動續跑。
#   已在執行則跳過，避免重複。
# ============================================================
DIR=/home/tearicee/data_collector/finmind
PY=/home/tearicee/data_collector/.venv/bin/python   # 共用 data_collector/.venv
TODAY=$(date +%F)
BLACKOUT="08:00-13:31"
CTL="$DIR/backfill_control.log"
ts(){ date '+%Y-%m-%d %H:%M:%S'; }

if pgrep -f "download_fundamental.py" >/dev/null; then
    echo "$(ts) [SKIP] 基本面 /data 回補 已在執行" >> "$CTL"
else
    nohup "$PY" "$DIR/download_fundamental.py" --end "$TODAY" --reverse \
        --blackout "$BLACKOUT" \
        > /dev/null 2>> "$DIR/download_fundamental.log" &
    echo "$(ts) [START] 基本面 /data 回補 pid=$!" >> "$CTL"
fi
