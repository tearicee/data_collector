"""
台灣指數公司「技術通知」每日下載器。

來源：https://taiwanindex.com.tw/downloads/technical_notice (Nuxt SSR，?page=N 分頁)
下載對象（依檔案類別）：
- 「定審結果」      → 成分股審核結果 → 解析進 constituents.csv
- 「指數定審期程表」→ 指數定期審核日程表 → 解析進 review_schedule.csv

每筆 PDF 存到 D 槽，並以 notice_id 去重（processed_ids.json）避免重複下載/重複寫入。

用法：
  python download_notice.py                 # 抓「今天」發布的目標報告
  python download_notice.py --date 20260626 # 抓指定日期
  python download_notice.py --days 7        # 抓最近 7 天（含今天）
  python download_notice.py --date 20260626 --force  # 忽略去重重抓
"""

import os
import re
import sys
import csv
import json
import time
import random
import logging
import argparse
from datetime import datetime, timedelta

import requests

import parse_notice
import parse_schedule

# ============================================================
# 設定
# ============================================================
if sys.platform == "win32":
    DATA_ROOT = r"D:\taiwanindex\technical_notice"
else:
    DATA_ROOT = "/mnt/d/taiwanindex/technical_notice"

PDF_DIR = os.path.join(DATA_ROOT, "pdf")
LOG_DIR = os.path.join(DATA_ROOT, "logs")
MANIFEST = os.path.join(DATA_ROOT, "processed_ids.json")
ADJUST_CSV = os.path.join(DATA_ROOT, "成分股調整紀錄.csv")
SCHEDULE_CSV = os.path.join(DATA_ROOT, "review_schedule.csv")
MAP_FILE = os.path.join(os.path.dirname(__file__), "index_etf_map.csv")

LIST_URL = "https://taiwanindex.com.tw/downloads/technical_notice?page={}"
DOWNLOAD_URL = "https://backend.taiwanindex.com.tw/api/downloadFile/TechnicalNotices/{}/tw"

# 目標檔案類別（兩類都下載 PDF；下方依類別各自轉成對應 CSV schema）
TARGET_CATEGORIES = {"定審結果", "指數定審期程表"}

# 成分股調整紀錄.csv 欄位（比照 成分股調整紀錄.xlsx A:G）
ADJUST_FIELDS = ["stock_id", "review_date", "public_date", "effective_date", "type", "etf", "is_new"]
# 指數定期審核日程表 CSV 欄位（前置來源追溯欄）
SCHEDULE_META = ["notice_id", "category", "file_date"]


def _norm(name: str) -> str:
    """指數名稱正規化：移除所有空白，供對照表比對（PDF 與人工輸入空白不一致）。"""
    return re.sub(r"\s+", "", name or "")


def load_index_etf_map() -> dict:
    """讀取 index_etf_map.csv → {正規化指數名稱: [etf, ...]}（一指數可能對應多 ETF）。"""
    m = {}
    if os.path.exists(MAP_FILE):
        with open(MAP_FILE, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                idx = _norm(row.get("index_name", ""))
                etf = (row.get("etf") or "").strip()
                if idx and etf:
                    m.setdefault(idx, []).append(etf)
    return m

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
]
REQUEST_TIMEOUT = (10, 30)
MAX_PAGES = 80  # 翻頁上限（迴圈會在掃到比目標範圍更舊的整頁時提早停止；放寬上限以支援回補）

# 解析列表頁每筆區塊：檔案日期 / 檔案名稱 / 檔案類別 / 下載 id
_ENTRY_RE = re.compile(
    r"檔案日期</th>\s*<td[^>]*>([\d/]+)</td>.*?"
    r"檔案名稱</th>\s*<td[^>]*>([^<]+)</td>.*?"
    r"檔案類別</th>\s*<td[^>]*>([^<]+)</td>.*?"
    r"downloadFile/TechnicalNotices/(\d+)/tw",
    re.S,
)


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(
                os.path.join(LOG_DIR, f"tip_{datetime.now():%Y%m%d}.log"),
                encoding="utf-8",
            ),
            logging.StreamHandler(),
        ],
    )


def _date_iso(slash_date: str) -> str:
    """'2026/06/26' → '2026-06-26'"""
    y, m, d = slash_date.split("/")
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|「」]', "", name).strip()[:80]


def load_manifest() -> dict:
    if os.path.exists(MANIFEST):
        with open(MANIFEST, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_manifest(m: dict):
    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)


def fetch_list(session: requests.Session, page: int) -> list[dict]:
    """抓單頁列表並回傳去重後的 entry 清單。"""
    r = session.get(
        LIST_URL.format(page),
        headers={"User-Agent": random.choice(USER_AGENTS)},
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    seen, entries = set(), []
    for date, title, cat, tid in _ENTRY_RE.findall(r.text):
        if tid in seen:  # 桌機/手機表格各一份，去重
            continue
        seen.add(tid)
        entries.append({
            "notice_id": tid,
            "file_date": _date_iso(date),
            "title": title.strip(),
            "category": cat.strip(),
        })
    return entries


def download_pdf(session: requests.Session, entry: dict) -> str:
    cat_dir = os.path.join(PDF_DIR, _safe(entry["category"]))
    os.makedirs(cat_dir, exist_ok=True)
    fname = f"{entry['file_date'].replace('-', '')}_{entry['notice_id']}_{_safe(entry['title'])}.pdf"
    path = os.path.join(cat_dir, fname)
    r = session.get(
        DOWNLOAD_URL.format(entry["notice_id"]),
        headers={"User-Agent": random.choice(USER_AGENTS)},
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    if not r.content.startswith(b"%PDF"):
        raise ValueError(f"下載內容非 PDF (notice_id={entry['notice_id']})")
    with open(path, "wb") as f:
        f.write(r.content)
    return path


_TYPE_MAP = {"納入": "add", "刪除": "delete"}


def _write_csv(csv_path: str, fields: list[str], rows: list[dict]):
    new_file = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
    with open(csv_path, "a", encoding="utf-8_sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerows(rows)


def write_constituents(parsed_rows: list[dict], idx_etf: dict) -> int:
    """成分股審核結果 → 成分股調整紀錄.csv（A:G）。

    依對照表把指數名稱換成 ETF 代號；一指數對應多 ETF 則展開成多列。
    無對照者 etf 留空（資料仍保留，便於日後補對照表）。
    """
    out = []
    for r in parsed_rows:
        etfs = idx_etf.get(_norm(r["index_name"]), [""])
        for etf in etfs:
            out.append({
                "stock_id": r["stock_id"],
                "review_date": r["review_date"],
                "public_date": r["announce_date"],
                "effective_date": r["effective_date"],
                "type": _TYPE_MAP.get(r["action"], r["action"]),
                "etf": etf,
                "is_new": False,
            })
    _write_csv(ADJUST_CSV, ADJUST_FIELDS, out)
    return len(out)


def write_schedule(parsed_rows: list[dict], meta: dict) -> int:
    fields = SCHEDULE_META + parse_schedule.SCHEDULE_FIELDS
    _write_csv(SCHEDULE_CSV, fields, [{**meta, **r} for r in parsed_rows])
    return len(parsed_rows)


def run(target_dates: set[str], force: bool):
    os.makedirs(PDF_DIR, exist_ok=True)
    manifest = load_manifest()
    idx_etf = load_index_etf_map()
    logging.info(f"指數→ETF 對照表載入 {len(idx_etf)} 筆指數")
    session = requests.Session()

    # 收集落在目標日期內、且為目標類別的 entries（翻頁直到頁面最舊日期早於目標範圍）
    min_date = min(target_dates)
    todo = []
    for page in range(1, MAX_PAGES + 1):
        entries = fetch_list(session, page)
        if not entries:
            break
        for e in entries:
            if e["category"] in TARGET_CATEGORIES and e["file_date"] in target_dates:
                todo.append(e)
        # 此頁全部都比目標範圍更舊 → 不必再往後翻
        if all(e["file_date"] < min_date for e in entries):
            break
        time.sleep(random.uniform(1, 2))

    logging.info(f"目標日期 {sorted(target_dates)}，符合的目標報告 {len(todo)} 筆")

    ok = skip = fail = 0
    for e in todo:
        if not force and e["notice_id"] in manifest:
            logging.info(f"[跳過] id={e['notice_id']} 已處理：{e['title']}")
            skip += 1
            continue
        try:
            pdf_path = download_pdf(session, e)
            if e["category"] == "定審結果":
                _, parsed = parse_notice.parse_pdf(pdf_path)
                n = write_constituents(parsed, idx_etf)
                dst = os.path.basename(ADJUST_CSV)
            else:  # 指數定審期程表
                _, parsed = parse_schedule.parse_pdf(pdf_path)
                meta = {"notice_id": e["notice_id"], "category": e["category"], "file_date": e["file_date"]}
                n = write_schedule(parsed, meta)
                dst = os.path.basename(SCHEDULE_CSV)
            manifest[e["notice_id"]] = {
                "title": e["title"], "category": e["category"],
                "file_date": e["file_date"], "pdf": pdf_path, "rows": n,
            }
            save_manifest(manifest)
            logging.info(
                f"[完成] id={e['notice_id']} {e['category']} {n} 列 → {dst}｜{e['title']}"
            )
            ok += 1
            time.sleep(random.uniform(2, 4))
        except Exception as ex:
            logging.error(f"[失敗] id={e['notice_id']} {e['title']}：{ex}")
            fail += 1

    logging.info(f"完成 {ok}、跳過 {skip}、失敗 {fail}")
    return ok, skip, fail


def parse_args():
    p = argparse.ArgumentParser(description="台灣指數公司技術通知下載器")
    p.add_argument("--date", help="指定日期 YYYYMMDD（預設今天）")
    p.add_argument("--days", type=int, default=1, help="往回涵蓋天數（含今天），預設 1")
    p.add_argument("--force", action="store_true", help="忽略去重，重抓重寫")
    return p.parse_args()


if __name__ == "__main__":
    setup_logging()
    args = parse_args()
    if args.date:
        base = datetime.strptime(args.date, "%Y%m%d")
    else:
        base = datetime.now()
    dates = {(base - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(args.days)}
    logging.info("=" * 60)
    logging.info(f"技術通知下載器啟動，涵蓋日期：{sorted(dates)}")
    run(dates, force=args.force)
