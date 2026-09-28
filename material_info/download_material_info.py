#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全市場「重大訊息」爬蟲 (MOPS 新版 API + TWSE/TPEx OpenAPI 對帳)
================================================================
主來源 (MOPS 新版 SPA 背後的 JSON API，皆 POST application/json)：
  - 清單: https://mops.twse.com.tw/mops/api/t05st02
          body {"year":"115","month":"9","day":"28"} (民國年，月/日不補零)
          → 「當日重大訊息」全日清單，含上市/上櫃/興櫃/公開發行四市場。
          注意：查詢某日會連帶回傳「前一日 17:30 後」的訊息，本程式一律依
          每筆的「發言日期」歸檔，重疊部分以 MOPS鍵 去重。
  - 內文: https://mops.twse.com.tw/mops/api/<apiName>
          apiName/body 取自清單每筆附的連結 {enterDate, marketKind, companyId, serialNumber}
          一般公司為 t05st02_detail → 發言人/職稱/電話/符合條款/事實發生日/說明；
          外國發行人 (DR) 為 t59sb01_detail，欄位不同 (說明在「發生依外國發行人…」、
          附件 PDF 在「其他」、事實發生日為「民國115年9月23日」)，於 fetch_detail 正規化。
  說明：首頁「即時重大訊息」(t05sr01_1) 用的是同一份資料，但 API 固定只回最新
  20 筆，尖峰時段輪詢會漏，故改用 t05st02 取全日清單。

對帳來源 (--mode daily 才跑)：
  - 上市 https://openapi.twse.com.tw/v1/opendata/t187ap04_L
  - 上櫃 https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O
  OpenAPI 只給「前一日」上市/上櫃的快照 (不含興櫃/公開發行)，拿來比對已存資料，
  MOPS 漏抓的以 來源=openapi 補入。陷阱：TWSE 鍵「主旨 」帶尾空白，TPEx 部分鍵
  為英文 (SecuritiesCompanyCode/CompanyName)，一律正規化。

模式：
  --mode poll   (預設) 輪詢今天清單，只抓新出現訊息的內文。cron 每 10 分鐘。
                網路失敗不立即告警，連續失敗達門檻才以 exit 1 讓 wrapper 告警一次。
  --mode daily  回掃最近 --days 天 (預設 3) + 重抓缺內文者 + OpenAPI 對帳。
  --start/--end 指定日期區間回補 (YYYY-MM-DD)。

輸出 (依發言日期月份分檔，Parquet zstd，冪等 upsert；見 store.py)：
  /mnt/d/mops/material_info/data/重大訊息_YYYY-MM.parquet
"""

import argparse
import json
import random
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

DC_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DC_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import heartbeat  # noqa: E402
import store  # noqa: E402

# --------------------------------------------------------------------------
STATE_DIR = store.STATE_DIR
POLL_FAIL_FILE = STATE_DIR / "poll_fail_count.json"
CHECKPOINT_EVERY = 50  # 每抓 N 筆內文就寫檔一次，中斷可續跑

MOPS_API = "https://mops.twse.com.tw/mops/api/"
OPENAPI_SOURCES = [
    ("上市", "https://openapi.twse.com.tw/v1/opendata/t187ap04_L"),
    ("上櫃", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O"),
]
MARKET_NAMES = {"sii": "上市", "otc": "上櫃", "rotc": "興櫃", "pub": "公開發行"}

# 連續失敗第 N 次告警，之後每 M 次再告警 (poll 每 10 分鐘 → 1 小時首告、之後每 6 小時)
POLL_ALERT_FIRST = 6
POLL_ALERT_EVERY = 36

SRC_MOPS = "mops"
SRC_MOPS_LIST_ONLY = "mops_list_only"   # 內文查無 (406)，只存清單欄位，daily 模式會重試
SRC_OPENAPI = "openapi"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://mops.twse.com.tw",
    "Referer": "https://mops.twse.com.tw/mops/",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


class NoData(Exception):
    """MOPS 回 查無相符資料 (非網路錯誤，不需重試)。"""


def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"{ts()} {msg}", flush=True)


# ------------------------------------------------------------------ 格式轉換
def roc_to_iso(s: str) -> str:
    """民國日期 '115/09/28'、'1150928'、'民國115年9月23日' → '2026-09-28'；無法解析原樣回傳。"""
    s = (s or "").strip()
    m = re.fullmatch(r"(?:民國)?(\d{2,3})年(\d{1,2})月(\d{1,2})日", s)
    if m:
        return f"{int(m[1]) + 1911:04d}-{int(m[2]):02d}-{int(m[3]):02d}"
    digits = s.replace("/", "")
    if "/" in s:
        parts = s.split("/")
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            return f"{int(parts[0]) + 1911:04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    elif len(digits) >= 7 and digits.isdigit():
        return f"{int(digits[:-4]) + 1911:04d}-{digits[-4:-2]}-{digits[-2:]}"
    return s


def norm_time(s: str) -> str:
    """'07:00:04' / '70004' / '070004' → '07:00:04'。"""
    s = (s or "").strip()
    if ":" in s:
        return s
    if s.isdigit():
        s = s.zfill(6)
        return f"{s[:2]}:{s[2:4]}:{s[4:6]}"
    return s


def clean_text(s) -> str:
    """統一換行為 \\n，去除前後空白。"""
    if s is None:
        return ""
    return str(s).replace("\r\n", "\n").replace("\r", "\n").strip()


def clean_subject(s) -> str:
    """主旨為定寬硬換行 (如 '...第1121801204\\r\\n號函辦理')，接回單行。"""
    return "".join(line.strip() for line in clean_text(s).split("\n"))


# ------------------------------------------------------------------ HTTP
def mops_post(api: str, body: dict, retries: int = 4, timeout: int = 30):
    """POST MOPS API，回 result；查無資料 raise NoData；其餘錯誤指數退避重試。"""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = SESSION.post(MOPS_API + api, json=body, timeout=timeout)
            resp.raise_for_status()
            data = json.loads(resp.content.decode("utf-8"))
            code = data.get("code")
            if code == 200:
                return data.get("result") or {}
            msg = data.get("message", "")
            if code == 406 or "查無" in msg:
                raise NoData(msg)
            raise ValueError(f"code={code} message={msg}")
        except NoData:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            wait = min(2 ** attempt, 30)
            log(f"  [WARN] {api} 第 {attempt}/{retries} 次失敗: {e}；{wait}s 後重試")
            time.sleep(wait)
    raise RuntimeError(f"{api} 失敗 body={body}: {last_err}")


def fetch_day_list(d: date) -> list:
    """t05st02：查詢日 d 的清單 (含前一日晚間訊息)。"""
    body = {"year": str(d.year - 1911), "month": str(d.month), "day": str(d.day)}
    try:
        result = mops_post("t05st02", body)
    except NoData:
        return []
    items = []
    for row in result.get("data") or []:
        # [發言日期, 發言時間, 公司代號, 公司名稱, 主旨, {apiName, parameters}]
        link = row[5] if len(row) > 5 and isinstance(row[5], dict) else {}
        p = link.get("parameters") or {}
        items.append({
            "發言日期": roc_to_iso(row[0]),
            "發言時間": norm_time(row[1]),
            "公司代號": str(row[2]).strip(),
            "公司簡稱": str(row[3]).strip(),
            "主旨": clean_subject(row[4]),
            "api": link.get("apiName") or "t05st02_detail",
            "params": p,
            "MOPS鍵": "-".join(str(p.get(k, "")) for k in
                               ("enterDate", "marketKind", "companyId", "serialNumber")),
            "市場別": MARKET_NAMES.get(p.get("marketKind", ""), p.get("marketKind", "")),
        })
    return items


def fetch_detail(api: str, params: dict) -> dict:
    """內文 API：依 titles 對應欄位，回 dict (DR 的 t59sb01_detail 轉成一般欄位名)。"""
    result = mops_post(api, params)
    titles = [t.get("main", "") for t in result.get("titles") or []]
    rows = result.get("data") or []
    if not rows:
        raise NoData("detail 無 data")
    # 通常只有一列；多列時取序號相符者
    serial = params.get("serialNumber")
    row = next((r for r in rows if serial in r[:2]), rows[0])
    d = dict(zip(titles, row))
    for k in list(d):
        if k.startswith("發生依外國發行人"):  # DR 的說明欄
            d["說明"] = d.pop(k)
    other = d.pop("其他", None)            # DR 附件 {fileName, url}
    if isinstance(other, dict) and other.get("url"):
        d["說明"] = f"{clean_text(d.get('說明'))}\n附件: {other['url']}".strip()
    elif isinstance(other, list):
        urls = [o.get("url") for o in other if isinstance(o, dict) and o.get("url")]
        if urls:
            d["說明"] = clean_text(d.get("說明")) + "".join(f"\n附件: {u}" for u in urls)
    return d


def fetch_openapi(url: str) -> list:
    last_err = None
    for attempt in range(1, 5):
        try:
            resp = SESSION.get(url, timeout=60)
            resp.raise_for_status()
            data = json.loads(resp.content.decode("utf-8"))
            if not isinstance(data, list):
                raise ValueError(f"回傳非 list (type={type(data).__name__})")
            return data
        except Exception as e:  # noqa: BLE001
            last_err = e
            wait = min(2 ** attempt, 30)
            log(f"  [WARN] OpenAPI 第 {attempt}/4 次失敗: {e}；{wait}s 後重試")
            time.sleep(wait)
    raise RuntimeError(f"OpenAPI 抓取失敗 {url}: {last_err}")


# ------------------------------------------------------------------ 組列
def build_row(item: dict, detail: dict | None, source: str) -> dict:
    detail = detail or {}
    t = norm_time(detail.get("發言時間") or item["發言時間"])
    d = roc_to_iso(detail.get("發言日期")) if detail.get("發言日期") else item["發言日期"]
    return {
        "公司代號": item["公司代號"],
        "公司簡稱": item["公司簡稱"],
        "發布時間": f"{d} {t}",
        "主旨": clean_subject(detail.get("主旨")) or item["主旨"],
        "說明": clean_text(detail.get("說明")),
        "市場別": item["市場別"],
        "發言日期": d,
        "發言時間": t,
        "符合條款": clean_text(detail.get("符合條款")),
        "事實發生日": roc_to_iso(clean_text(detail.get("事實發生日"))),
        "發言人": clean_text(detail.get("發言人")),
        "發言人職稱": clean_text(detail.get("發言人職稱")),
        "發言人電話": clean_text(detail.get("發言人電話")),
        "來源": source,
        "MOPS鍵": item["MOPS鍵"],
        "抓取時間": ts(),
    }


# ------------------------------------------------------------------ 主流程
def fetch_details(todo: list, stats: dict, sleep=(0.8, 1.5), before_each=None,
                  detail_fn=None) -> None:
    """逐筆抓內文並分批 upsert。查無內文先存清單欄位；網路錯誤不存 (下次重試)。
    before_each: 每次請求前呼叫 (回補用來插入長暫停/冷卻)；detail_fn 預設 fetch_detail。
    中途例外也會先把已抓的寫檔。"""
    detail_fn = detail_fn or fetch_detail
    buf = []
    try:
        for n, it in enumerate(todo, 1):
            if before_each:
                before_each(n)
            time.sleep(random.uniform(*sleep))
            try:
                row = build_row(it, detail_fn(it["api"], it["params"]), SRC_MOPS)
            except NoData:
                row = build_row(it, None, SRC_MOPS_LIST_ONLY)
                stats["list_only"] += 1
                log(f"  [WARN] 內文查無，先存清單欄位: {it['公司代號']} {it['主旨'][:30]}")
            except Exception as e:  # noqa: BLE001
                stats["failed"].append(f"{it['公司代號']}@{it['發言日期']} {it['發言時間']}")
                stats["consec_fail"] = stats.get("consec_fail", 0) + 1
                log(f"  [ERROR] 內文抓取失敗 {it['MOPS鍵']}: {e}")
                continue
            stats["consec_fail"] = 0
            if not it.get("_retry"):
                stats["new"] += 1
                log(f"  [NEW] {row['發布時間']} {row['公司代號']} {row['公司簡稱']} {row['主旨'][:40]}")
            buf.append(row)
            if len(buf) >= CHECKPOINT_EVERY:
                stats["months_written"].update(store.upsert(buf))
                log(f"  [SAVE] 進度 {n}/{len(todo)}")
                buf = []
    finally:
        if buf:
            stats["months_written"].update(store.upsert(buf))


def pending_items(items: list, retry_list_only: bool) -> list:
    """濾出尚未存檔 (或需重抓內文) 的訊息。"""
    by_month: dict = {}
    for it in items:
        by_month.setdefault(it["發言日期"][:7], []).append(it)
    todo = []
    for ym, its in sorted(by_month.items()):
        have = store.existing(ym)
        for it in its:
            old = have.get(it["MOPS鍵"])
            if old is None:
                todo.append(it)
            elif retry_list_only and old.get("來源") == SRC_MOPS_LIST_ONLY:
                todo.append(dict(it, _retry=True))
    return todo


def sync_mops(query_dates: list, retry_list_only: bool, stats: dict) -> None:
    """抓各查詢日清單 → 去重 → 新訊息抓內文 → upsert。"""
    uniq: dict = {}
    for qd in query_dates:
        items = fetch_day_list(qd)
        log(f"---- 清單 {qd}: {len(items)} 筆")
        stats["listed"] += len(items)
        for it in items:
            uniq[it["MOPS鍵"]] = it
    todo = pending_items(list(uniq.values()), retry_list_only)
    if todo:
        log(f"  不重複 {len(uniq)} 筆，待抓內文 {len(todo)} 筆")
        fetch_details(todo, stats)


def reconcile_openapi(stats: dict) -> None:
    """用 OpenAPI 前一日上市/上櫃快照比對，MOPS 漏的以 來源=openapi 補入。"""
    add = []
    have: dict = {}
    for market, url in OPENAPI_SOURCES:
        raw = fetch_openapi(url)
        for r in raw:
            c = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in r.items()}
            item = {
                "公司代號": c.get("公司代號") or c.get("SecuritiesCompanyCode", ""),
                "公司簡稱": c.get("公司名稱") or c.get("CompanyName", ""),
                "發言日期": roc_to_iso(c.get("發言日期", "")),
                "發言時間": norm_time(c.get("發言時間", "")),
                "主旨": clean_subject(c.get("主旨")),
                "市場別": market,
                "MOPS鍵": "",
            }
            ym = item["發言日期"][:7]
            if ym not in have:
                have[ym] = {(r["公司代號"], r["發布時間"]) for r in store.load_month(ym)}
            key = (item["公司代號"], f"{item['發言日期']} {item['發言時間']}")
            if key in have[ym]:
                continue
            detail = {"說明": c.get("說明"), "符合條款": c.get("符合條款"),
                      "事實發生日": c.get("事實發生日")}
            add.append(build_row(item, detail, SRC_OPENAPI))
            have[ym].add(key)
            log(f"  [GAP] MOPS 缺，OpenAPI 補入: {key[1]} {item['公司代號']} {item['主旨'][:40]}")
        log(f"---- OpenAPI {market}: {len(raw)} 筆")
        stats["openapi_checked"] += len(raw)
    if add:
        stats["months_written"].update(store.upsert(add))
        stats["openapi_filled"] += len(add)


def date_range(start: date, end: date) -> list:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def poll_fail_count(update: str) -> int:
    """update='reset' 歸零 / 'inc' 加一，回傳目前連續失敗次數。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    if update == "inc":
        try:
            n = json.loads(POLL_FAIL_FILE.read_text(encoding="utf-8")).get("count", 0)
        except Exception:  # noqa: BLE001
            n = 0
        n += 1
    POLL_FAIL_FILE.write_text(json.dumps({"count": n, "updated": ts()}), encoding="utf-8")
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="MOPS 重大訊息爬蟲")
    ap.add_argument("--mode", choices=["poll", "daily"], default="poll")
    ap.add_argument("--days", type=int, default=None,
                    help="回掃天數 (含今天)；poll 預設 1、daily 預設 3")
    ap.add_argument("--start", help="回補起日 YYYY-MM-DD (與 --end 併用，覆蓋 --days)")
    ap.add_argument("--end", help="回補迄日 YYYY-MM-DD (預設今天)")
    ap.add_argument("--no-openapi", action="store_true", help="daily 模式不做 OpenAPI 對帳")
    args = ap.parse_args()

    today = date.today()
    if args.start:
        end = date.fromisoformat(args.end) if args.end else today
        query_dates = date_range(date.fromisoformat(args.start), end)
    else:
        n = args.days or (1 if args.mode == "poll" else 3)
        query_dates = date_range(today - timedelta(days=n - 1), today)

    job = "material_info" if args.mode == "poll" else "material_info_daily"
    stats = {"listed": 0, "new": 0, "list_only": 0, "openapi_checked": 0,
             "openapi_filled": 0, "failed": [], "months_written": set()}
    log(f"==== 重大訊息 {args.mode} 開始 查詢日 {query_dates[0]}~{query_dates[-1]} ====")

    try:
        sync_mops(query_dates, retry_list_only=(args.mode == "daily"), stats=stats)
        if args.mode == "daily" and not args.no_openapi:
            reconcile_openapi(stats)
    except Exception as e:  # noqa: BLE001
        log(f"[ERROR] {e}")
        if args.mode == "poll":
            n = poll_fail_count("inc")
            alert = n == POLL_ALERT_FIRST or (
                n > POLL_ALERT_FIRST and (n - POLL_ALERT_FIRST) % POLL_ALERT_EVERY == 0)
            log(f"  poll 連續失敗 {n} 次{'，達告警門檻' if alert else '，暫不告警'}")
            heartbeat.write(job, heartbeat.STATUS_FAIL, 1, f"連續失敗 {n} 次: {e}",
                            stats={"consecutive_failures": n})
            return 1 if alert else 0
        raise

    if args.mode == "poll":
        poll_fail_count("reset")
    stats["months_written"] = sorted(stats["months_written"])
    heartbeat.write(job, heartbeat.STATUS_OK, 0,
                    f"清單 {stats['listed']} 筆，新增 {stats['new']} 筆", stats=stats)
    log(f"==== 完成 清單={stats['listed']} 新增={stats['new']} 缺內文={stats['list_only']} "
        f"OpenAPI補={stats['openapi_filled']} 失敗={len(stats['failed'])} "
        f"寫入={stats['months_written']} ====")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        log(f"[FATAL] {e}")
        sys.exit(1)
