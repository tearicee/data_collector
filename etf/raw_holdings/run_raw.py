#!/usr/bin/env python3
"""投信原始持股爬蟲 — 主控。

流程:
  1. 決定 raw 追蹤清單 (第一期 = 現有 28 檔對應的主要發行商；預設讀
     fund_codes.json 全母體，但只有『已實作 adapter 的發行商』才會實抓)
  2. 由 issuer_map 取代號→發行商
  3. 發行商已有 adapter → fetch + normalize + 存 D:\\etf_daily_holdings_raw\\<發行商>\\
     否則 → 記入 pending_adapters.json (待補 adapter，不算失敗)
  4. 寫心跳 (job=etf_raw)；已實作 adapter 拋非預期例外才算 failed 並告警

輸出: D:\\etf_daily_holdings_raw\\<發行商>\\YYYYMMDD_<code>.csv  (WSL: /mnt/d/...)

用法:
  python raw_holdings/run_raw.py --now
  python raw_holdings/run_raw.py --codes 0050,00878 --now
  python raw_holdings/run_raw.py --selftest    # 框架自我驗證 (寫一筆示範資料)
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

RAW_DIR_PARENT = Path(__file__).resolve().parent
sys.path.insert(0, str(RAW_DIR_PARENT))                 # raw_holdings/
sys.path.insert(0, str(RAW_DIR_PARENT.parent))          # etf/
sys.path.insert(0, str(RAW_DIR_PARENT.parent.parent))   # data_collector/

import base                                             # noqa: E402
from base import REGISTRY, NotSupportedYet, StubAdapter, NORMALIZED_COLUMNS  # noqa: E402
import issuer_map                                       # noqa: E402
import issuers  # noqa: E402,F401  觸發 adapter 註冊

if sys.platform == "win32":
    RAW_DIR = Path(r"D:\etf_daily_holdings_raw")
    FUND_CODES_JSON = Path(r"D:\etf_daily_holdings\fund_master\fund_codes.json")
else:
    RAW_DIR = Path("/mnt/d/etf_daily_holdings_raw")
    FUND_CODES_JSON = Path("/mnt/d/etf_daily_holdings/fund_master/fund_codes.json")

PENDING_JSON = RAW_DIR / "pending_adapters.json"

# 第一期 raw 追蹤範圍：現有 28 檔 (與 etf_crawler.FALLBACK_STOCK_IDS 對齊)
TRACKED_28 = [
    "00982A", "00981A", "00878", "00919", "00918", "00940", "00929", "00713",
    "00939", "00881", "00900", "0050", "0056", "00934", "00932", "00923",
    "00915", "00936", "00733", "0057", "00403A", "0052", "00891", "00991A",
    "00913", "00992A", "00405A", "00400A",
]

SLEEP_BETWEEN = 3.0     # 各基金間隔 (秒)，禮貌節流


def _apply_exclusions(codes: list[str]) -> list[str]:
    """套用共用排除清單 (etf/exclusions.py)：排除商品/發行商不抓、不計 pending。"""
    try:
        import exclusions
        kept, dropped = exclusions.filter_codes(codes)
        if dropped:
            print(f"[raw] 排除 {len(dropped)} 檔 (exclusions.py): {dropped}")
        return kept
    except Exception as e:                               # noqa: BLE001
        print(f"[raw] 套用排除清單失敗 ({e})，維持原名單", file=sys.stderr)
        return codes


def load_codes(cli_codes: str | None) -> list[str]:
    if cli_codes:
        # 手動指定的代號尊重使用者意圖，不套排除
        return [c.strip() for c in cli_codes.split(",") if c.strip()]
    # 預設跑全母體：有 adapter 的發行商全部 ETF 都抓 raw，其餘歸 pending。
    try:
        codes = json.loads(FUND_CODES_JSON.read_text(encoding="utf-8"))
        if codes:
            return _apply_exclusions(list(codes))
    except Exception:
        pass
    return _apply_exclusions(list(TRACKED_28))          # 主檔讀不到時的保底


def _save_one(df: pd.DataFrame, issuer: str, code: str, date: str) -> Path:
    out_dir = RAW_DIR / issuer
    out_dir.mkdir(parents=True, exist_ok=True)
    # 以 df 內來源實際交易日命名 (adapter 可能回傳與傳入 date 不同的 trandate)
    actual = date
    if "date" in df.columns and len(df) and str(df["date"].iloc[0]).strip():
        actual = str(df["date"].iloc[0]).strip()
    path = out_dir / f"{actual}_{code}.csv"
    df.to_csv(path, encoding="utf-8-sig", index=False)
    return path


def run(codes: list[str], date: str) -> dict:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    imap = issuer_map.all_map()

    done: list[str] = []
    pending: dict[str, str] = {}          # code -> issuer (待補 adapter)
    failed: list[str] = []
    nodata: list[str] = []

    # 按發行商分組：同一家的檔連續處理，處理完立即 close 釋放資源。
    # 這確保任一時刻只有一個 playwright adapter (群益/中信) 活著，
    # 避免兩個 sync_playwright 實例在同 process 衝突 (asyncio loop 錯誤)。
    groups: dict[str, list[str]] = {}
    for code in codes:
        groups.setdefault(imap.get(code, "未知"), []).append(code)

    for issuer, codes_g in groups.items():
        adapter = REGISTRY.get(issuer)
        if adapter is None or isinstance(adapter, StubAdapter):
            for code in codes_g:
                pending[code] = issuer
            continue
        for code in codes_g:
            try:
                raw = adapter.fetch(code, date)
                if raw is None or len(raw) == 0:
                    nodata.append(code)
                else:
                    df = adapter.normalize(raw, code, date)
                    path = _save_one(df, issuer, code, date)
                    print(f"[raw] {issuer} {code} → {len(df)} 筆, {path}")
                    done.append(code)
            except NotSupportedYet:
                pending[code] = issuer
            except ValueError as e:                      # 查無資料 / 尚未公告
                print(f"[raw] {issuer} {code} 無資料: {e}")
                nodata.append(code)
            except Exception as e:                        # noqa: BLE001
                print(f"[raw][error] {issuer} {code} 失敗: {e}", file=sys.stderr)
                failed.append(code)
            time.sleep(SLEEP_BETWEEN)
        # 該發行商處理完，立即釋放資源 (playwright browser 及時關閉)
        try:
            adapter.close()
        except Exception:                                # noqa: BLE001
            pass

    # 待補 adapter 清單 (供健檢摘要)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_JSON.write_text(json.dumps(pending, ensure_ascii=False, indent=2),
                            encoding="utf-8")

    # 釋放 adapter 資源 (如 headless browser)
    for ad in REGISTRY.values():
        try:
            ad.close()
        except Exception:                            # noqa: BLE001
            pass

    pending_issuers = sorted(set(pending.values()))
    print(f"[raw] 完成 done:{len(done)} nodata:{len(nodata)} "
          f"failed:{len(failed)} pending:{len(pending)} "
          f"(待補發行商 {len(pending_issuers)}: {pending_issuers})")

    _report(done, nodata, failed, pending, pending_issuers)
    return {"done": done, "nodata": nodata, "failed": failed,
            "pending": pending, "pending_issuers": pending_issuers}


def _report(done, nodata, failed, pending, pending_issuers):
    try:
        from common import heartbeat
        heartbeat.write(
            "etf_raw",
            status="fail" if failed else "ok",
            exit_code=0,
            message=f"done {len(done)} / pending {len(pending)}",
            stats={"done": len(done), "nodata": len(nodata),
                   "failed": failed, "pending": len(pending),
                   "pending_issuers": pending_issuers},
        )
    except Exception:
        pass
    if failed:
        try:
            from common import notify_discord
            notify_discord.alert(
                "etf_raw",
                f"投信原始持股爬蟲有 {len(failed)} 檔失敗：{', '.join(failed)}",
                exit_code=None)
        except Exception:
            pass


def selftest(date: str):
    """框架自我驗證：用假 adapter 產一筆 normalize 資料並存檔，驗證欄位/路徑。"""
    class _Demo(base.IssuerAdapter):
        def fetch(self, fund_code, date):
            return pd.DataFrame({
                "stock_id": ["2330", "2317"],
                "name": ["台積電", "鴻海"],
                "shares": [1000, 500],
                "weight": [45.0, 12.0],
            })
    demo = _Demo("示範發行商")
    raw = demo.fetch("DEMO", date)
    df = demo.normalize(raw, "DEMO", date)
    assert list(df.columns) == NORMALIZED_COLUMNS, df.columns
    path = _save_one(df, "_selftest", "DEMO", date)
    print(f"[selftest] 正規化欄位 OK: {NORMALIZED_COLUMNS}")
    print(f"[selftest] 已寫入 {path}")
    print(df.to_string(index=False))


def main():
    ap = argparse.ArgumentParser(description="投信原始持股爬蟲")
    ap.add_argument("--codes", help="覆寫追蹤清單，逗號分隔")
    ap.add_argument("--date", help="資料日期 YYYYMMDD (預設今天)")
    ap.add_argument("--now", action="store_true", help="(相容用) 立即執行")
    ap.add_argument("--selftest", action="store_true", help="框架自我驗證")
    args = ap.parse_args()

    date = args.date or datetime.now().strftime("%Y%m%d")
    if args.selftest:
        selftest(date)
        return
    codes = load_codes(args.codes)
    run(codes, date)


if __name__ == "__main__":
    main()
