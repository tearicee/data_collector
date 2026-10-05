#!/usr/bin/env python3
"""ETF 申購買回清單 (PCF) 資料集的儲存層。

資料放 D:\\etf_pcf\\ (WSL: /mnt/d/etf_pcf):
    creation_redemption.parquet     每日申購贖回 (全部 ETF 一張表，每檔每日一列)
    constituents/<代號>.parquet      每日成分 (每檔一張長表)
    source/                          匯入的原始歷史壓縮檔

欄位沿用 2026-10-02 那份歷史資料 (前 50 大 ETF，自各檔成立日起) 的中文欄名，另加「資料基準日」。

日期欄位的意義 —— 讀資料前務必看這段:
  「交易日」是歷史資料原有的鍵，本資料集照舊沿用，但它在各投信、兩張表的意義不同:
    申購贖回    富邦: 淨值/單位數所屬的日子      其餘六家: 清單公告日 (內容是前一交易日收盤後的數字)
    每日成分    國泰/富邦/復華: 持股所屬的日子    元大/統一/群益/中信: 清單公告日 (持股是前一交易日的)
               另外，資料來源為「申購買回清單…」「PCF公告持股」的列一律是公告日。
  例: 國泰 00878 的「交易日 2026-09-30」，申購贖回那列是 09-29 的淨值，成分卻是 09-30 的持股。
  「資料基準日」把這件事寫明: 這一列的數字反映哪一個交易日收盤後的狀態。跨投信比較、
  對價格、算流量都應該用「資料基準日」，不要用「交易日」。「基準日依據」說明它怎麼來的:
    來源   投信 API 回應自帶的資料日 (元大 trandate、國泰 preDateC、統一 TranDate、群益 date2、
           中信 淨值日期、富邦/復華/國泰持股明細頁的資料日期)。收集器抓的列盡量是這種。
    推定   依上面的規則回推。只用在國內型 ETF (universe.csv 的「市場」= 國內)；歷史列都是這種。
           規則以淨值對收盤價 (申購贖回，逐檔逐年) 與股數×收盤價重算權重 (成分，逐檔逐季抽樣)
           驗證過整段歷史。
    (空)   海外與債券型的歷史列。這類基金的淨值日會再落後 (例: 公告日 2026-09-30 的美債 ETF，
           資料日是 09-24)，落後幾天隨海外假期而變，回推不可靠，所以留空而不是填一個可能錯的日期。
"""
from __future__ import annotations
import glob
import os
import sys
from pathlib import Path

import pandas as pd

if sys.platform == "win32":
    PCF_DIR = Path(r"D:\etf_pcf")
    PRICE_DIR = Path(r"D:\finmind_data\TaiwanStockPrice")
else:
    PCF_DIR = Path("/mnt/d/etf_pcf")
    PRICE_DIR = Path("/mnt/d/finmind_data/TaiwanStockPrice")

UNIVERSE_CSV = Path(__file__).resolve().parent / "universe.csv"

SUMMARY_COLUMNS = ["市值排名", "代號", "名稱", "投信", "交易日", "公告日期", "資料基準日", "基準日依據", "狀態",
                   "申購贖回淨增減單位數", "已發行單位數", "每單位淨值", "基金淨資產", "實物申贖基數", "來源網址"]
SUMMARY_NUMERIC = ["申購贖回淨增減單位數", "已發行單位數", "每單位淨值", "基金淨資產", "實物申贖基數"]
CONSTITUENT_COLUMNS = ["ETF代號", "ETF名稱", "投信", "交易日", "資料基準日", "基準日依據", "資料來源", "類別",
                       "成分代碼", "成分名稱", "數量", "金額", "權重(%)", "到期月份"]
CONSTITUENT_NUMERIC = ["數量", "金額", "權重(%)"]

HAS_DATA, NO_DATA = "有資料", "來源無資料"
BY_SOURCE, BY_RULE = "來源", "推定"
DOMESTIC = "國內"
# 原歷史檔沒有、由本資料集加上的欄位
ADDED_COLUMNS = ("資料基準日", "基準日依據")

# 「交易日」就是資料基準日的投信；其餘為清單公告日 (基準日 = 前一交易日)
SUMMARY_SAME_DAY = {"富邦"}
CONSTITUENT_SAME_DAY = {"國泰", "富邦", "復華"}
PCF_SOURCES = ("申購買回清單", "PCF公告持股")       # 這些資料來源的成分列一律是公告日


def summary_path(root: Path | None = None) -> Path:
    return (root or PCF_DIR) / "creation_redemption.parquet"


def constituent_path(code: str, root: Path | None = None) -> Path:
    return (root or PCF_DIR) / "constituents" / f"{code}.parquet"


def load_universe() -> pd.DataFrame:
    """追蹤清單: 市值排名, 代號, 名稱, 投信, 市場 (國內/海外/債券)。要加減檔數改 universe.csv 即可。"""
    return pd.read_csv(UNIVERSE_CSV, encoding="utf-8-sig", dtype=str)


# --------------------------------------------------------------------------- #
# 交易日曆與資料基準日
# --------------------------------------------------------------------------- #
def price_days(price_dir: Path | None = None) -> list[str]:
    """有收盤價檔的日子 (YYYY-MM-DD，由舊到新)。"""
    out = set()
    for f in glob.glob(str((price_dir or PRICE_DIR) / "*" / "TaiwanStockPrice_*.parquet")):
        d = Path(f).stem.split("_", 1)[1]
        if len(d) == 10:
            out.add(d)
    return sorted(out)


class Calendar:
    """交易日序列；用來求某日的前一個交易日。"""

    def __init__(self, days):
        self.days = sorted({str(d) for d in days if isinstance(d, str) and len(d) == 10})

    def prev(self, day: str) -> str | None:
        """嚴格早於 day 的最近一個交易日；day 本身不必在序列裡 (例如尚未開市的下一個公告日)。"""
        import bisect
        i = bisect.bisect_left(self.days, day)
        return self.days[i - 1] if i > 0 else None


def summary_asof(issuer: str, market: str, trade_day: str, cal: Calendar) -> str | None:
    """依規則推定申購贖回列的資料基準日；非國內型不推定 (回 None)。"""
    if market != DOMESTIC:
        return None
    return trade_day if issuer in SUMMARY_SAME_DAY else cal.prev(trade_day)


def constituent_asof(issuer: str, market: str, source: str, trade_day: str, cal: Calendar) -> str | None:
    """依規則推定成分列的資料基準日；非國內型不推定 (回 None)。"""
    if market != DOMESTIC:
        return None
    same_day = issuer in CONSTITUENT_SAME_DAY and not str(source).startswith(PCF_SOURCES)
    return trade_day if same_day else cal.prev(trade_day)


# --------------------------------------------------------------------------- #
# 讀寫
# --------------------------------------------------------------------------- #
def _atomic_write(df: pd.DataFrame, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def normalize_summary(df: pd.DataFrame) -> pd.DataFrame:
    df = df.reindex(columns=SUMMARY_COLUMNS).copy()
    for c in SUMMARY_NUMERIC:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["市值排名"] = pd.to_numeric(df["市值排名"], errors="coerce").astype("Int64")
    for c in ("代號", "名稱", "投信", "交易日", "公告日期", "資料基準日", "基準日依據", "狀態", "來源網址"):
        df[c] = df[c].astype("string")
    return df


def normalize_constituents(df: pd.DataFrame) -> pd.DataFrame:
    df = df.reindex(columns=CONSTITUENT_COLUMNS).copy()
    for c in CONSTITUENT_NUMERIC:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in ("ETF代號", "ETF名稱", "投信", "交易日", "資料基準日", "基準日依據", "資料來源", "類別", "成分代碼",
              "成分名稱", "到期月份"):
        df[c] = df[c].astype("string")
    return df


def load_summary(root: Path | None = None) -> pd.DataFrame:
    p = summary_path(root)
    if not p.exists():
        return normalize_summary(pd.DataFrame(columns=SUMMARY_COLUMNS))
    return pd.read_parquet(p)


def load_constituents(code: str, root: Path | None = None) -> pd.DataFrame:
    p = constituent_path(code, root)
    if not p.exists():
        return normalize_constituents(pd.DataFrame(columns=CONSTITUENT_COLUMNS))
    return pd.read_parquet(p)


def upsert_summary(rows: pd.DataFrame, root: Path | None = None) -> int:
    """以 (代號, 交易日) 為鍵寫入；同鍵以新的為準。回傳新增或有變動的列數。"""
    if rows is None or len(rows) == 0:
        return 0
    new = normalize_summary(rows)
    old = load_summary(root)
    merged = pd.concat([old, new], ignore_index=True).drop_duplicates(["代號", "交易日"], keep="last")
    merged = merged.sort_values(["市值排名", "代號", "交易日"], kind="stable").reset_index(drop=True)
    key = ["代號", "交易日"]
    before = old.merge(new[key], on=key, how="inner")
    changed = len(new) - len(before.merge(new, on=SUMMARY_COLUMNS, how="inner"))
    if changed:
        _atomic_write(merged, summary_path(root))
    return changed


def upsert_constituents(code: str, rows: pd.DataFrame, root: Path | None = None) -> int:
    """某檔 ETF 的成分: rows 內出現的「交易日」整日換新。回傳寫入的交易日數 (內容相同的不算)。"""
    if rows is None or len(rows) == 0:
        return 0
    new = normalize_constituents(rows)
    old = load_constituents(code, root)
    days = sorted(new["交易日"].dropna().unique())
    changed = 0
    for d in days:
        a = old[old["交易日"] == d].reset_index(drop=True)
        b = new[new["交易日"] == d].reset_index(drop=True)
        if not (len(a) == len(b) and a.astype("string").fillna("").equals(b.astype("string").fillna(""))):
            changed += 1
    if not changed:
        return 0
    keep = old[~old["交易日"].isin(days)]
    merged = pd.concat([keep, new], ignore_index=True).sort_values("交易日", kind="stable").reset_index(drop=True)
    _atomic_write(merged, constituent_path(code, root))
    return changed


def calendar(root: Path | None = None, price_dir: Path | None = None) -> Calendar:
    """交易日曆 = 有收盤價檔的日子 ∪ 申購贖回表裡不晚於最新收盤價日的交易日。

    申購贖回表會有日期在未來的列 (下一個交易日的公告清單)，那些日子還沒開市，不算進日曆。"""
    days = set(price_days(price_dir))
    s = load_summary(root)
    if len(s):
        known = set(s["交易日"].dropna().astype(str))
        last = max(days) if days else max(known)
        days |= {d for d in known if d <= last}
    return Calendar(days)
