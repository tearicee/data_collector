"""一次性爬取台灣指數公司技術通知「全部頁面」清單，快取到 JSON。

用於歷史回補：先把所有 entry（notice_id/file_date/title/category）抓下來，
之後可離線分析、依標題比對指數名稱，只下載需要的 PDF。
"""
import sys, json, time, random
import requests
from download_notice import fetch_list

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/tip_all_entries.json"

def main():
    s = requests.Session()
    all_entries = []
    seen = set()
    page = 1
    while page <= 200:
        try:
            entries = fetch_list(s, page)
        except Exception as ex:
            print(f"page {page} error: {ex}", flush=True)
            time.sleep(3)
            continue
        if not entries:
            print(f"page {page} empty -> stop", flush=True)
            break
        for e in entries:
            if e["notice_id"] in seen:
                continue
            seen.add(e["notice_id"])
            all_entries.append(e)
        if page % 10 == 0:
            print(f"page {page}: total {len(all_entries)} entries, last {entries[-1]['file_date']}", flush=True)
        time.sleep(random.uniform(0.6, 1.2))
        page += 1
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(all_entries, f, ensure_ascii=False, indent=1)
    print(f"DONE: {len(all_entries)} entries -> {OUT}", flush=True)

if __name__ == "__main__":
    main()
