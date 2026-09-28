#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重大訊息歷史回補 — MOPS「重大訊息主旨全文檢索」(t51sb10_q1) 市場別查詢
================================================================
流程 (與網頁操作相同：由市場別查詢 → 選上市/上櫃 → 民國年度 + 月份 + 起迄日)：
  1. POST https://mops.twse.com.tw/mops/api/redirectToOld
       {"apiName":"ajax_t51sb10","parameters":{r1:1, KIND:L|O, year:115, month1, begin_day, end_day, ...}}
     → 回舊站結果網址 (mopsov.twse.com.tw/mops/web/ajax_t51sb10?parameters=<加密>)
  2. GET 該網址取第 1 頁 (預設 15 筆) + 表單隱藏欄位，再以 PCount=100 逐頁 POST 翻頁。
     每列的隱藏欄 h{i}0~h{i}9：簡稱/代號/發言日期/發言時間/主旨/序號/市場別/條款/enterDate/?
     → h08=enterDate、h05=serialNumber、h06=marketKind，與 t05st02 清單參數完全一致，
       因此 MOPS鍵 與每日爬蟲共用、內文沿用 JSON API t05st02_detail。
  3. 逐筆抓內文 (已存者跳過) → store.upsert()，每 50 筆存一次，可隨時中斷續跑。
  限制：查詢條件只能單一月份，跨月區間自動切段；只含上市(L)/上櫃(O)。

爬蟲禮儀 (避免被擋)：
  - 單一 Session、一般瀏覽器 UA + Referer，不併發。
  - 翻頁間隔 3~6 秒；內文間隔 1.5~3 秒，每 100 筆長休 30~60 秒。
  - 被擋訊號 (HTTP 403/429/5xx、安全性考量頁、非 JSON) → 指數退避；
    內文連續失敗 5 筆 → 冷卻 10 分鐘，冷卻 3 次仍失敗即中止 (已存資料保留，重跑續傳)。
  - 清單存 state/ 快取，重跑不必重新翻頁。

用法：
  python backfill_t51sb10.py --start 2026-08-28 --end 2026-09-27
  python backfill_t51sb10.py --start 2026-08-28 --end 2026-09-27 --markets L --relist
"""

import argparse
import html
import json
import random
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import download_material_info as dmi  # noqa: E402
import store  # noqa: E402
from download_material_info import NoData, log  # noqa: E402

REDIRECT_API = dmi.MOPS_API + "redirectToOld"
OLD_LIST_URL = "https://mopsov.twse.com.tw/mops/web/ajax_t51sb10"
KIND_NAMES = {"L": "上市", "O": "上櫃"}
BLOCK_MARKERS = ("安全性考量", "FOR SECURITY REASONS", "CAN NOT BE ACCESSED", "查詢過於頻繁")

PAGE_SLEEP = (3.0, 6.0)
DETAIL_SLEEP = (1.5, 3.0)
LONG_PAUSE_EVERY = 100
LONG_PAUSE = (30, 60)
COOLDOWN_AFTER = 5          # 內文連續失敗筆數
COOLDOWN_SECONDS = 600
MAX_COOLDOWNS = 3


class Blocked(Exception):
    pass


def polite_request(method: str, url: str, retries: int = 5, **kw):
    """帶退避的請求；遇被擋訊號等待更久。回 response。"""
    last = None
    for attempt in range(1, retries + 1):
        try:
            resp = dmi.SESSION.request(method, url, timeout=60, **kw)
            text_head = resp.content[:4000].decode("utf-8", "ignore")
            if resp.status_code in (403, 429) or any(m in text_head for m in BLOCK_MARKERS):
                wait = int(resp.headers.get("Retry-After") or 0) or 120 * attempt
                raise Blocked(f"HTTP {resp.status_code} 疑似被擋，{wait}s 後重試")
            resp.raise_for_status()
            return resp
        except Blocked as e:
            last = e
            log(f"  [BLOCK] {e}")
            time.sleep(wait)
        except Exception as e:  # noqa: BLE001
            last = e
            wait = min(5 * 2 ** attempt, 300)
            log(f"  [WARN] {method} 第 {attempt}/{retries} 次失敗: {e}；{wait}s 後重試")
            time.sleep(wait)
    raise RuntimeError(f"{method} {url[:80]} 失敗: {last}")


def month_segments(start: date, end: date):
    """切成單月區段 [(y, m, d1, d2)]。"""
    cur = start
    while cur <= end:
        nxt_month = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
        seg_end = min(end, nxt_month - timedelta(days=1))
        yield cur.year, cur.month, cur.day, seg_end.day
        cur = seg_end + timedelta(days=1)


def parse_rows(page_html: str, kind: str) -> list:
    """解析結果頁每列隱藏欄 h{i}{j} → 與 fetch_day_list 相同結構的 item。"""
    rows: dict = {}
    for k, v in re.findall(r"name='h(\d+)' value='([^']*)'", page_html):
        rows.setdefault(k[:-1], {})[k[-1]] = html.unescape(v)
    items = []
    for r in rows.values():
        spoke, enter = r.get("2", ""), r.get("8", "")
        if not (spoke.isdigit() and enter.isdigit()):
            continue
        roc_enter = f"{int(enter[:4]) - 1911}{enter[4:]}"
        params = {"enterDate": roc_enter, "marketKind": r.get("6", ""),
                  "companyId": r.get("1", "").strip(), "serialNumber": int(r.get("5", "0"))}
        items.append({
            "發言日期": f"{spoke[:4]}-{spoke[4:6]}-{spoke[6:]}",
            "發言時間": dmi.norm_time(r.get("3", "")),
            "公司代號": params["companyId"],
            "公司簡稱": r.get("0", "").strip(),
            "主旨": dmi.clean_subject(r.get("4", "")),
            "api": "t05st02_detail",
            "params": params,
            "MOPS鍵": "-".join(str(params[k]) for k in
                               ("enterDate", "marketKind", "companyId", "serialNumber")),
            "市場別": dmi.MARKET_NAMES.get(params["marketKind"], KIND_NAMES[kind]),
        })
    return items


def list_segment(kind: str, y: int, m: int, d1: int, d2: int) -> list:
    body = {"apiName": "ajax_t51sb10", "parameters": {
        "r1": "1", "KIND": kind, "CODE": "", "keyWord": "", "Condition2": "1",
        "keyWord2": "", "year": str(y - 1911), "month1": str(m),
        "begin_day": str(d1), "end_day": str(d2), "Orderby": "2",  # 由舊到新
        "encodeURIComponent": 1, "step": 1, "Stp": 4, "firstin": True, "off": 1, "go": False}}
    res = json.loads(polite_request("POST", REDIRECT_API, json=body).content.decode("utf-8"))
    if res.get("code") != 200:
        raise RuntimeError(f"redirectToOld 失敗: {res}")
    url = res["result"]["url"]
    time.sleep(random.uniform(*PAGE_SLEEP))
    first = polite_request("GET", url).content.decode("utf-8")
    if "查無" in first and "myTable" not in first:
        return []
    form = first[first.find("name='fm'"):]
    fields = dict(re.findall(r"<input type='hidden' name='([^']+)' value='([^']*)'>",
                             form[:form.find("PCount")]))
    fields.update({"PCount": "100"})

    items, page, pages = [], 1, 1
    while page <= pages:
        time.sleep(random.uniform(*PAGE_SLEEP))
        fields["pagenum"] = str(page)
        t = polite_request("POST", OLD_LIST_URL, data=fields,
                           headers={"Referer": url}).content.decode("utf-8")
        nums = [int(x) for x in re.findall(r"pagenum.value='(\d+)'", t)]
        pages = max(nums + [1])
        got = parse_rows(t, kind)
        items.extend(got)
        log(f"    {KIND_NAMES[kind]} {y}-{m:02d}-{d1:02d}~{d2:02d} 第 {page}/{pages} 頁 {len(got)} 筆")
        page += 1
    return items


def fetch_detail_any(api: str, params: dict) -> dict:
    """清單看不出是否為外國發行人：t05st02_detail 查無時改試 DR 的 t59sb01_detail。"""
    try:
        return dmi.fetch_detail(api, params)
    except NoData:
        return dmi.fetch_detail("t59sb01_detail", params)


def main() -> int:
    ap = argparse.ArgumentParser(description="t51sb10 重大訊息回補 (上市/上櫃)")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--markets", default="L,O", help="L=上市, O=上櫃")
    ap.add_argument("--relist", action="store_true", help="忽略清單快取重新翻頁")
    args = ap.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    kinds = [k.strip().upper() for k in args.markets.split(",") if k.strip()]

    log(f"==== t51sb10 回補 {start}~{end} 市場 {kinds} ====")
    all_items: dict = {}
    for kind in kinds:
        cache = store.STATE_DIR / f"t51sb10_{kind}_{start}_{end}.json"
        if cache.exists() and not args.relist:
            items = json.loads(cache.read_text(encoding="utf-8"))
            log(f"---- {KIND_NAMES[kind]} 使用清單快取 {len(items)} 筆 ({cache.name})")
        else:
            items = []
            for seg in month_segments(start, end):
                items.extend(list_segment(kind, *seg))
            store.STATE_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
            log(f"---- {KIND_NAMES[kind]} 清單 {len(items)} 筆")
        for it in items:
            all_items[it["MOPS鍵"]] = it

    todo = dmi.pending_items(list(all_items.values()), retry_list_only=True)
    log(f"==== 不重複 {len(all_items)} 筆，已存 {len(all_items) - len(todo)}，待抓內文 {len(todo)} 筆 "
        f"(預估 {len(todo) * 2.6 / 60:.0f} 分鐘) ====")

    stats = {"listed": len(all_items), "new": 0, "list_only": 0, "failed": [],
             "months_written": set(), "consec_fail": 0}
    cooldowns = [0]

    def before_each(n: int) -> None:
        if n > 1 and (n - 1) % LONG_PAUSE_EVERY == 0:
            p = random.uniform(*LONG_PAUSE)
            log(f"  [PAUSE] 已抓 {n - 1} 筆，休息 {p:.0f}s")
            time.sleep(p)
        if stats["consec_fail"] >= COOLDOWN_AFTER:
            cooldowns[0] += 1
            if cooldowns[0] > MAX_COOLDOWNS:
                raise RuntimeError("連續失敗且冷卻多次仍未恢復，中止 (重跑可續傳)")
            log(f"  [COOLDOWN] 連續失敗 {stats['consec_fail']} 筆，冷卻 {COOLDOWN_SECONDS}s "
                f"({cooldowns[0]}/{MAX_COOLDOWNS})")
            time.sleep(COOLDOWN_SECONDS)
            stats["consec_fail"] = 0

    dmi.fetch_details(todo, stats, sleep=DETAIL_SLEEP, before_each=before_each,
                      detail_fn=fetch_detail_any)

    log(f"==== 完成 新增={stats['new']} 缺內文={stats['list_only']} 失敗={len(stats['failed'])} "
        f"寫入={sorted(stats['months_written'])} ====")
    if stats["failed"]:
        log(f"  失敗清單 (重跑會補): {stats['failed'][:20]}")
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        log(f"[FATAL] {e}")
        sys.exit(1)
