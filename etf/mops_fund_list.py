#!/usr/bin/env python3
"""MOPS 基金基本資料彙總表爬蟲 (全市場 ETF 主檔)。

資料來源: 公開資訊觀測站 t51sb11「基金基本資料彙總表」
  POST https://mopsov.twse.com.tw/mops/web/ajax_t51sb11
  body: step=0&firstin=true&TYPEK=sii&run=   (TYPEK 任意值都回全部)
  回傳 UTF-8 HTML 單表，pandas.read_html 解析 → 約 263 檔 × 28 欄。

用途:
  1. 建立全市場 ETF 主檔 (代號/簡稱/類型/名稱/成立日/上市日/發行商…)
  2. 每日快照存 D 槽並同步 gdrive
  3. 產出 fund_codes.json — 供 etf_crawler.py 驅動抓取名單 (全母體)
  4. diff 前一份快照，偵測新掛牌 / 下市代號並發 Discord 通知

輸出 (WSL /mnt/d，Windows D:):
  fund_master/基金基本資料彙總表_YYYYMMDD.csv   每日快照
  fund_master/fund_master_latest.csv            最新正規檔
  fund_master/fund_codes.json                   全部代號 list

用法:
  python etf/mops_fund_list.py --now
  python etf/mops_fund_list.py --date 20260706   # 覆寫快照命名日期 (測試 diff)
"""
from __future__ import annotations
import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

# 重用 etf_crawler 的重試 Session 與 UA 池
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from etf_crawler import build_session, USER_AGENTS  # noqa: E402
import random  # noqa: E402

# ============================================================
# 設定
# ============================================================
if sys.platform == "win32":
    MASTER_DIR = Path(r"D:\etf_daily_holdings\fund_master")
else:
    MASTER_DIR = Path("/mnt/d/etf_daily_holdings/fund_master")

MOPS_URL = "https://mopsov.twse.com.tw/mops/web/ajax_t51sb11"
MOPS_REFERER = "https://mopsov.twse.com.tw/mops/web/t51sb11_q2"
POST_DATA = {"step": "0", "firstin": "true", "TYPEK": "sii", "run": ""}
REQUEST_TIMEOUT = (10, 30)

CODE_RE = re.compile(r"^\d{4,6}[A-Z]?$")

LATEST_CSV = "fund_master_latest.csv"
CODES_JSON = "fund_codes.json"
SNAPSHOT_FMT = "基金基本資料彙總表_{date}.csv"


def fetch_master() -> pd.DataFrame:
    """抓取並解析 MOPS 基金主檔，回傳去空白欄名的 DataFrame。"""
    session = build_session()
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Referer": MOPS_REFERER,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    res = session.post(MOPS_URL, data=POST_DATA, headers=headers,
                       timeout=REQUEST_TIMEOUT)
    res.raise_for_status()
    res.encoding = "utf-8"
    tables = pd.read_html(io.StringIO(res.text), encoding="utf-8")
    # 找含「基金代號」的表
    df = None
    for t in tables:
        cols = [re.sub(r"\s", "", str(c)) for c in t.columns]
        if any("基金代號" in c for c in cols):
            t.columns = cols
            df = t
            break
    if df is None:
        raise ValueError("MOPS 回傳中找不到含『基金代號』的表格")
    df = df.dropna(how="all")
    # 去除各欄位值的前後空白 / &nbsp;
    for c in df.columns:
        if df[c].dtype == object:
            df[c] = df[c].astype(str).str.replace("\xa0", "", regex=False).str.strip()
    df["基金代號"] = df["基金代號"].astype(str).str.strip()
    df = df[df["基金代號"].str.match(CODE_RE)].reset_index(drop=True)
    return df


def load_prev_codes(today_snapshot: Path) -> set[str] | None:
    """讀取「上一份」快照 (日期最新且非本次) 的代號集合。無前份回 None。"""
    snaps = sorted(MASTER_DIR.glob("基金基本資料彙總表_*.csv"))
    snaps = [p for p in snaps if p.name != today_snapshot.name]
    if not snaps:
        return None
    prev = snaps[-1]
    try:
        pdf = pd.read_csv(prev, encoding="utf-8-sig", dtype=str)
        return set(pdf["基金代號"].astype(str).str.strip())
    except Exception:                                # noqa: BLE001
        return None


def main():
    date_str = datetime.now().strftime("%Y%m%d")
    if "--date" in sys.argv:
        i = sys.argv.index("--date")
        if i + 1 < len(sys.argv):
            date_str = sys.argv[i + 1]

    MASTER_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[mops] 抓取基金主檔… ({datetime.now():%Y-%m-%d %H:%M:%S})")
    df = fetch_master()
    codes = df["基金代號"].tolist()
    print(f"[mops] 解析成功: {len(df)} 檔基金 × {df.shape[1]} 欄")

    snapshot = MASTER_DIR / SNAPSHOT_FMT.format(date=date_str)
    prev_codes = load_prev_codes(snapshot)           # 覆寫前先讀舊份

    # 寫快照 + 最新檔 + 代號清單
    df.to_csv(snapshot, encoding="utf-8-sig", index=False)
    df.to_csv(MASTER_DIR / LATEST_CSV, encoding="utf-8-sig", index=False)
    (MASTER_DIR / CODES_JSON).write_text(
        json.dumps(codes, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"[mops] 已寫入 {snapshot.name} / {LATEST_CSV} / {CODES_JSON}")

    # diff 偵測新增 / 移除
    new_codes: list[str] = []
    removed_codes: list[str] = []
    if prev_codes is not None:
        cur = set(codes)
        new_codes = sorted(cur - prev_codes)
        removed_codes = sorted(prev_codes - cur)
        if new_codes or removed_codes:
            print(f"[mops] 代號異動 — 新增 {new_codes} / 移除 {removed_codes}")
            _notify_change(df, new_codes, removed_codes)
    else:
        print("[mops] 無前一份快照，首建主檔 (不報代號異動)")

    # 寫心跳 (資料層)
    _write_heartbeat(len(df), new_codes, removed_codes)


def _notify_change(df: pd.DataFrame, new_codes, removed_codes):
    """新掛牌 / 下市代號 → Discord 通知 (事件，非失敗)。"""
    try:
        from common import notify_discord
    except Exception:                                # noqa: BLE001
        return
    lines = ["🆕 **MOPS 基金主檔代號異動**"]
    if new_codes:
        lines.append(f"\n新增 {len(new_codes)} 檔:")
        for c in new_codes:
            row = df[df["基金代號"] == c]
            name = row["基金簡稱"].iloc[0] if len(row) else ""
            typ = row["基金類型"].iloc[0] if len(row) and "基金類型" in df.columns else ""
            lines.append(f"  {c}  {name}  ({typ})")
    if removed_codes:
        lines.append(f"\n移除 {len(removed_codes)} 檔: {', '.join(removed_codes)}")
    lines.append("\n→ 已自動納入 fund_codes.json，今日起 CMoney 爬蟲一併抓取。")
    try:
        notify_discord.post("\n".join(lines), code_block=False)
    except Exception as e:                            # noqa: BLE001
        print(f"[mops][warn] 代號異動通知送出失敗: {e}", file=sys.stderr)


def _write_heartbeat(total, new_codes, removed_codes):
    try:
        from common import heartbeat
        heartbeat.write("mops_fund_list", status="ok", exit_code=0,
                        message="主檔更新成功",
                        stats={"total": total, "new": new_codes,
                               "removed": removed_codes})
    except Exception:                                # noqa: BLE001
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception as e:                            # noqa: BLE001
        print(f"[mops][error] 主檔爬蟲失敗: {e}", file=sys.stderr)
        try:
            from common import heartbeat
            heartbeat.write("mops_fund_list", status="fail", exit_code=1,
                            message=str(e))
        except Exception:                            # noqa: BLE001
            pass
        sys.exit(1)
