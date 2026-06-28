#!/bin/bash
# ============================================================
# 每日更新 FinMind「台股 - 籌碼面」資料集 (直到 token 失效為止)
#   /data 端點 (download_chip.py)：13 個籌碼面資料集
#     逐日       : 個股融資融劵 MarginPurchaseShortSale、三大法人買賣 InstitutionalInvestorsBuySell(+Wide)、
#                 外資持股 Shareholding、股權持股分級 HoldingSharesPer、借券成交 SecuritiesLending、
#                 信用額度餘額 DailyShortSaleBalances、借貸擔保品餘額 LoanCollateralBalance、
#                 當沖借券費率 DayTradingBorrowingFeeRate
#     整段快照   : 大盤融資維持率 TotalExchangeMarginMaintenance、處置證券 DispositionSecuritiesPeriod、
#                 暫停融券賣出 MarginShortSaleSuspension
#     整表快照   : 證券商資訊 SecuritiesTraderInfo
#   storage_objects (download_tick.py)：TaiwanStockWarrantTradingDailyReport (權證分點，sponsor)
#
# 來源更新時間多為盤後 Mon-Fri 21:00，故本腳本排深夜/凌晨，40 天視窗可補延遲發布並自動續補缺口。
# 流程：1) 檢查 token 仍為有效 SponsorPro，否則停止  2) 補抓最近視窗 (skip-existing 續補)
# 各下載器內建防封鎖：402 配額暫停 + 連續失敗熔斷 + 每請求最小間隔 + 401/403 立即中止。
# 仍帶 --blackout 08:00-14:30 保險 (排凌晨理論上不觸發)。
# ============================================================
PROJECT_DIR=/home/tearicee/data_collector/finmind
VENV_PY=/home/tearicee/data_collector/.venv/bin/python   # 共用 data_collector/.venv
LOG_DIR=/mnt/d/finmind_data/logs
LOG_FILE="$LOG_DIR/daily_chip.log"
BLACKOUT="08:00-14:30"

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR" || exit 1
TS() { date '+%Y-%m-%d %H:%M:%S'; }

# --- 1. token 檢查 (失效即停止) ---
CHECK=$("$VENV_PY" check_token.py 2>>"$LOG_FILE")
if [ "$CHECK" != "OK" ]; then
    echo "$(TS) [STOP] token 已失效或降級，停止下載: $CHECK" >> "$LOG_FILE"
    exit 0
fi

# --- 避免重複執行 ---
if pgrep -f "download_chip.py" >/dev/null; then
    echo "$(TS) [SKIP] 籌碼面更新 已在執行" >> "$LOG_FILE"
    exit 0
fi

# --- 2. 最近視窗 (40 天可補延遲發布並自動補回 06-16 以來缺口) ---
START=$(date -d '40 days ago' +%F)
END=$(date +%F)
echo "$(TS) [INFO] token 有效，更新籌碼面資料集，視窗 $START ~ $END" >> "$LOG_FILE"

# /data 全部籌碼面 13 集
"$VENV_PY" download_chip.py --start "$START" --end "$END" --reverse \
    --blackout "$BLACKOUT" >> "$LOG_FILE" 2>&1

# storage_objects 權證分點
"$VENV_PY" download_tick.py --dataset TaiwanStockWarrantTradingDailyReport \
    --start "$START" --end "$END" --reverse --blackout "$BLACKOUT" >> "$LOG_FILE" 2>&1

echo "$(TS) [INFO] 籌碼面每日更新流程結束" >> "$LOG_FILE"
