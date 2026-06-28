"""從本機 manifest(processed_ids.json)重建「追蹤 ETF 成分股異動全史」CSV。

與 backfill_history.py 不同：不爬網、不下載，只讀已下載 PDF。供每晚 download_notice
增量抓完新通知後，重新產生最新的全史 CSV(含更名鏈比對 + xlsx 成立初始成分股)。
"""
import os, json, csv, collections, logging

import parse_notice
from download_notice import load_manifest, _TYPE_MAP, DATA_ROOT, _norm, setup_logging
from etf_index_lineage import match_etf, LINEAGE
from backfill_history import xlsx_founding_rows, FIELDS, OUT_CSV, RAW_JSON


def manifest_rows():
    man = load_manifest()
    rows = []
    miss = 0
    for nid, v in man.items():
        if v.get("category") != "定審結果":
            continue
        hits = match_etf(_norm(v.get("title", "")))
        if not hits:
            continue
        pdf = v.get("pdf")
        if not pdf or not os.path.exists(pdf):
            miss += 1
            continue
        meta, prows = parse_notice.parse_pdf(pdf)
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
    if miss:
        logging.warning(f"manifest 有 {miss} 筆定審結果 PDF 遺失，已略過")
    return rows


def main():
    setup_logging()
    rows = manifest_rows()
    founding = xlsx_founding_rows()
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
    by = collections.Counter(r["etf"] for r in final)
    logging.info(f"重建完成 {len(final)} 列 → {OUT_CSV}｜{dict(sorted(by.items()))}")


if __name__ == "__main__":
    main()
