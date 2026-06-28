"""TIP 追蹤 ETF「成分股異動」全史回補。

流程：
1. 讀取已快取的全部技術通知清單(tip_all_entries.json)。
2. 篩出 category=定審結果、且標題比對到 15 支追蹤 ETF(含更名鏈, etf_index_lineage)。
3. 逐筆下載 PDF(已在 manifest 且檔案存在者沿用，不重抓)，以 parse_notice 解析納入/刪除。
4. 產出 A:G 列(stock_id, review_date, public_date, effective_date, type, etf, is_new=False)。
5. 併入 xlsx 中 is_new=TRUE 的成立初始成分股(網站定審結果不含),以 (stock_id,public_date,type,etf) 去重。
6. 寫出與 成分股調整紀錄.xlsx 相同欄位的 CSV。

用法: python backfill_history.py
"""
import os, sys, json, time, random, csv, collections, datetime, logging

import requests
import openpyxl

import parse_notice
from download_notice import (download_pdf, load_manifest, save_manifest,
                             _TYPE_MAP, DATA_ROOT, _norm, setup_logging)
from etf_index_lineage import LINEAGE, match_etf

CACHE = "/tmp/claude-1000/-home-tearicee-data-collector/f44984f2-4535-4e2e-9912-627f125f9e9a/scratchpad/tip_all_entries.json"
XLSX = os.path.join(DATA_ROOT, "成分股調整紀錄.xlsx")
OUT_CSV = os.path.join(DATA_ROOT, "追蹤ETF成分股調整_全史.csv")
RAW_JSON = os.path.join(DATA_ROOT, "追蹤ETF成分股調整_全史_raw.json")
FIELDS = ["stock_id", "review_date", "public_date", "effective_date", "type", "etf", "is_new"]


def collect_targets():
    entries = [e for e in json.load(open(CACHE, encoding="utf-8")) if e["category"] == "定審結果"]
    todo = []
    for e in entries:
        hits = match_etf(_norm(e["title"]))
        if hits:
            todo.append((e, hits))
    todo.sort(key=lambda t: t[0]["file_date"])
    return todo


def web_rows(todo):
    manifest = load_manifest()
    session = requests.Session()
    rows = []
    ok = reuse = fail = 0
    for e, hits in todo:
        nid = e["notice_id"]
        try:
            cached = manifest.get(nid, {})
            pdf = cached.get("pdf")
            if pdf and os.path.exists(pdf):
                reuse += 1
            else:
                pdf = download_pdf(session, e)
                ok += 1
                time.sleep(random.uniform(2, 4))
            meta, prows = parse_notice.parse_pdf(pdf)
            n = 0
            for etf in hits:
                for r in prows:
                    rows.append({
                        "stock_id": r["stock_id"],
                        "review_date": meta["review_date"],
                        "public_date": meta["announce_date"],
                        "effective_date": meta["effective_date"],
                        "type": _TYPE_MAP.get(r["action"], r["action"]),
                        "etf": etf,
                        "is_new": False,
                    })
                    n += 1
            manifest[nid] = {"title": e["title"], "category": e["category"],
                             "file_date": e["file_date"], "pdf": pdf, "rows": cached.get("rows", n)}
            save_manifest(manifest)
            logging.info(f"[OK] {e['file_date']} id={nid} {hits} 解析 {len(prows)} 檔｜{e['title'][:40]}")
        except Exception as ex:
            fail += 1
            logging.error(f"[FAIL] id={nid} {e['title'][:40]}：{ex}")
    logging.info(f"web 下載 {ok}、沿用 {reuse}、失敗 {fail}，共 {len(rows)} 列")
    return rows


def xlsx_founding_rows():
    """xlsx 中 is_new=TRUE 且 etf 屬於追蹤清單的成立初始成分股。"""
    wb = openpyxl.load_workbook(XLSX, read_only=True)
    ws = wb["調整"]
    data = list(ws.iter_rows(min_row=1, values_only=True))
    hdr = data[0]
    def f(x):
        return x.strftime("%Y-%m-%d") if isinstance(x, datetime.datetime) else (x if x not in (None, "None") else "")
    out = []
    for r in data[1:]:
        d = dict(zip(hdr, r))
        etf = str(d["etf"])
        if etf not in LINEAGE:
            continue
        if d["is_new"] not in (True, 1):
            continue
        out.append({
            "stock_id": str(d["stock_id"]),
            "review_date": f(d["review_date"]),
            "public_date": f(d["public_date"]),
            "effective_date": f(d["effective_date"]),
            "type": d["type"],
            "etf": etf,
            "is_new": True,
        })
    logging.info(f"xlsx 成立初始成分股(is_new) {len(out)} 列")
    return out


def main():
    setup_logging()
    todo = collect_targets()
    logging.info(f"比對到目標定審結果 {len(todo)} 筆")
    rows = web_rows(todo)
    founding = xlsx_founding_rows()

    # 合併去重：key=(stock_id, public_date, type, etf)；is_new 優先保留 True
    merged = {}
    for r in rows + founding:
        key = (str(r["stock_id"]), r["public_date"], r["type"], r["etf"])
        if key in merged:
            if r["is_new"]:
                merged[key]["is_new"] = True
        else:
            merged[key] = dict(r)

    final = list(merged.values())
    final.sort(key=lambda x: (x["etf"], x["public_date"], x["type"], str(x["stock_id"])))

    with open(OUT_CSV, "w", encoding="utf-8_sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(final)
    json.dump(final, open(RAW_JSON, "w", encoding="utf-8"), ensure_ascii=False)
    logging.info(f"完成：{len(final)} 列 → {OUT_CSV}")

    # 摘要
    by = collections.Counter(r["etf"] for r in final)
    for etf in sorted(by):
        print(f"  {etf} {LINEAGE[etf][0]:<10} {by[etf]} 列", flush=True)
    print("TOTAL", len(final), flush=True)


if __name__ == "__main__":
    main()
