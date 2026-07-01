#!/bin/bash
# ============================================================
# 每日更新 FinMind「台股 - 可轉債」資料集 (直到 token 失效為止)
#   /data 端點 (download_convertible_bond.py)：
#     整表快照 : TaiwanStockConvertibleBondInfo (可轉債總覽，每次重抓覆寫)
#     逐日     : TaiwanStockConvertibleBondDaily (日成交)、
#                TaiwanStockConvertibleBondInstitutionalInvestors (三大法人)、
#                TaiwanStockConvertibleBondDailyOverview (每日總覽)
#   皆 Backer/Sponsor「不帶 data_id 取該日全市場」，1 請求/日。
#
# 流程：1) 檢查 token 仍有效  2) 補抓最近 40 天視窗 (skip-existing 續補延遲發布)
# 內建防封鎖：402 配額暫停 + 連續失敗熔斷 + 每請求最小間隔 + 401/403 立即中止 + 原子寫檔。
# 仍帶 --blackout 08:00-13:31 保險 (排凌晨不會觸發)。已在執行則跳過。
# ============================================================
PROJECT_DIR=/home/tearicee/data_collector/finmind
VENV_PY=/home/tearicee/data_collector/.venv/bin/python   # 共用 data_collector/.venv
LOG_DIR=/mnt/d/finmind_data/logs
LOG_FILE="$LOG_DIR/daily_convertible_bond.log"
BLACKOUT="08:00-13:31"

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR" || exit 1
TS() { date '+%Y-%m-%d %H:%M:%S'; }

CHECK=$("$VENV_PY" check_token.py 2>>"$LOG_FILE")
if [ "$CHECK" != "OK" ]; then
    echo "$(TS) [STOP] token 已失效或降級，停止下載: $CHECK" >> "$LOG_FILE"
    exit 0
fi

if pgrep -f "download_convertible_bond.py" >/dev/null; then
    echo "$(TS) [SKIP] 可轉債更新 已在執行" >> "$LOG_FILE"
    exit 0
fi

START=$(date -d '40 days ago' +%F)
END=$(date +%F)
echo "$(TS) [INFO] token 有效，更新可轉債資料集，視窗 $START ~ $END" >> "$LOG_FILE"

"$VENV_PY" download_convertible_bond.py --start "$START" --end "$END" --reverse \
    --blackout "$BLACKOUT" >> "$LOG_FILE" 2>&1

echo "$(TS) [INFO] 可轉債每日更新流程結束" >> "$LOG_FILE"
