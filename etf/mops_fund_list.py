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
LISTED_STATE = "listed_notified.json"      # 已通知過掛牌的代號集合
LISTING_WINDOW_DAYS = 14                    # 只通知掛牌日在近 N 天內者 (避免補資料誤報舊檔)


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

    # 依「上市日期」偵測最近掛牌上市/上櫃的 ETF → Discord 通知
    ref_date = datetime.strptime(date_str, "%Y%m%d").date()
    listed = _notify_new_listings(df, ref_date)

    # 寫心跳 (資料層)
    _write_heartbeat(len(df), new_codes, removed_codes, listed)


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


def _roc_to_date(s):
    """民國日期字串 (YYY/MM/DD) → datetime.date；無法解析回 None。"""
    from datetime import date
    m = re.match(r"\s*(\d{2,3})/(\d{1,2})/(\d{1,2})\s*$", str(s))
    if not m:
        return None
    y, mth, d = int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3))
    try:
        return date(y, mth, d)
    except ValueError:
        return None


def _notify_new_listings(df: pd.DataFrame, ref_date) -> list[str]:
    """依『上市日期』找最近掛牌上市/上櫃的 ETF，去重後發 Discord。回傳本次通知代號。

    - 狀態檔 listed_notified.json 記已通知代號，避免重複洗頻。
    - 首次執行 (無狀態檔) 只把「目前已掛牌」全部納入基線、不通知，之後才報新掛牌。
    - 僅通知掛牌日在近 LISTING_WINDOW_DAYS 天內者，避免補歷史資料時誤報舊檔。
    """
    from datetime import timedelta
    if "上市日期" not in df.columns:
        return []
    state_path = MASTER_DIR / LISTED_STATE
    try:
        notified = set(json.loads(state_path.read_text(encoding="utf-8")))
    except Exception:                                # noqa: BLE001
        notified = None                              # None = 首次 (建基線)

    # 掃出所有「已掛牌」(上市日期 <= ref_date) 的代號
    listed_all: dict[str, object] = {}
    for _, row in df.iterrows():
        d = _roc_to_date(row.get("上市日期"))
        if d and d <= ref_date:
            listed_all[str(row["基金代號"]).strip()] = d

    if notified is None:                             # 首次執行：建基線、不通知
        state_path.write_text(json.dumps(sorted(listed_all), ensure_ascii=False),
                              encoding="utf-8")
        print(f"[mops] 首次建立掛牌基線 {len(listed_all)} 檔 (不通知)")
        return []

    window_start = ref_date - timedelta(days=LISTING_WINDOW_DAYS)
    fresh = [c for c, d in listed_all.items()
             if c not in notified and d >= window_start]
    fresh.sort(key=lambda c: (listed_all[c], c))

    # 無論是否落在通知窗，都把已掛牌者納入基線 (避免窗外舊檔日後補報)
    notified.update(listed_all)
    state_path.write_text(json.dumps(sorted(notified), ensure_ascii=False),
                          encoding="utf-8")

    if fresh:
        print(f"[mops] 最近掛牌上市/上櫃 {len(fresh)} 檔: {fresh}")
        _post_new_listings(df, fresh, listed_all)
    return fresh


def _post_new_listings(df: pd.DataFrame, codes, listed_dates):
    """最近掛牌 ETF → Discord 事件通知。"""
    try:
        from common import notify_discord
    except Exception:                                # noqa: BLE001
        return
    lines = [f"📢 **ETF 掛牌上市/上櫃通知** — {len(codes)} 檔"]
    for c in codes:
        row = df[df["基金代號"] == c]
        name = row["基金簡稱"].iloc[0] if len(row) else ""
        typ = row["基金類型"].iloc[0] if len(row) and "基金類型" in df.columns else ""
        d = listed_dates.get(c)
        lines.append(f"  `{c}`  {name}  掛牌日 {d}  ({typ})")
    try:
        notify_discord.post("\n".join(lines), code_block=False)
    except Exception as e:                            # noqa: BLE001
        print(f"[mops][warn] 掛牌通知送出失敗: {e}", file=sys.stderr)


def _write_heartbeat(total, new_codes, removed_codes, listed=None):
    try:
        from common import heartbeat
        heartbeat.write("mops_fund_list", status="ok", exit_code=0,
                        message="主檔更新成功",
                        stats={"total": total, "new": new_codes,
                               "removed": removed_codes,
                               "listed": listed or []})
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
