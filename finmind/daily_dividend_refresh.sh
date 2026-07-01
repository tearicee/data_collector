#!/bin/bash
# ============================================================
# 每日更新 FinMind「股利」資料 (sponsor 單日全市場端點，輕量)
#   per_stock 類資料在 download_fundamental.py 為 skip-existing，新公告股利不會自動進來。
#   本腳本改用 update_dividend.py：每天只掃日期窗的交易日，每日一個請求取全市場 (sponsor 專屬)，
#   再 merge 進 by_stock。約一兩百請求/日，負擔極小。涵蓋三個 per_stock 集 (各自視窗在 py 內設定)：
#     股利政策表 Dividend / 除權除息結果表 DividendResult (回看10+前看210，抓未來除息日)、
#     市值比重 MarketValueWeight (回看45+前看3，月底發布無未來資料)。
#
# 流程：
#   1. 檢查 token 仍為有效 SponsorPro，過期/降級則跳過 (不打 API)
#   2. update_dividend.py 掃描日期窗、merge 進 by_stock
#   內建防封鎖：402/額度自動暫停、每請求最小間隔、401/403 立即中止、原子寫檔。
#   --blackout 08:00-13:31：排凌晨理論上不觸發，保險保留。已在執行則跳過。
# ============================================================
DIR=/home/tearicee/data_collector/finmind
PY=/home/tearicee/data_collector/.venv/bin/python   # 共用 data_collector/.venv
BLACKOUT="08:00-13:31"
CTL="$DIR/backfill_control.log"
ts(){ date '+%Y-%m-%d %H:%M:%S'; }

cd "$DIR" || exit 1

# --- token 檢查：失效即跳過，不打 API ---
CHECK=$("$PY" check_token.py 2>>"$DIR/update_dividend.log")
if [ "$CHECK" != "OK" ]; then
    echo "$(ts) [SKIP] 股利更新：token 失效或降級 ($CHECK)，跳過" >> "$CTL"
    exit 0
fi

# --- 避免重複執行 ---
if pgrep -f "update_dividend.py" >/dev/null; then
    echo "$(ts) [SKIP] 股利更新 已在執行" >> "$CTL"
    exit 0
fi

echo "$(ts) [START] 股利更新 (update_dividend.py)" >> "$CTL"
"$PY" "$DIR/update_dividend.py" --back 10 --forward 210 --blackout "$BLACKOUT" \
    >> "$DIR/update_dividend.log" 2>&1
echo "$(ts) [DONE] 股利更新結束 rc=$?" >> "$CTL"
