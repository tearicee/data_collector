"""
解析台灣指數公司「指數定期審核日程表」PDF，輸出為 CSV 列。

輸出欄位：
- report_title   報告標題 (如「2026年7月指數定期審核日程表」)
- update_date    更新日期 (YYYY-MM-DD)
- index_name     指數名稱
- announce_date  公告日期 (YYYY-MM-DD)
- effective_date 生效日期 (YYYY-MM-DD)
"""

import re
import pdfplumber


def _iso(text: str) -> str:
    m = re.search(r"(20\d{2})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})", text)
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""


def parse_pdf(pdf_path: str) -> tuple[dict, list[dict]]:
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)

    title_m = re.search(r"(20\d{2}\s*年\s*\d{1,2}\s*月.*?日程表)", text)
    report_title = re.sub(r"\s+", "", title_m.group(1)) if title_m else ""

    upd_m = re.search(r"更新日期\s*[:：]\s*([\d/]+)", text)
    update_date = _iso(upd_m.group(1)) if upd_m else ""

    meta = {"report_title": report_title, "update_date": update_date}

    # 逐行擷取「指數名稱 公告日期 生效日期」；行內可能有兩個日期
    rows = []
    for line in text.splitlines():
        dates = re.findall(r"20\d{2}/\d{1,2}/\d{1,2}", line)
        if len(dates) < 2:
            continue
        # 指數名稱 = 第一個日期之前的文字（去除標題/雜訊）
        name = line[: line.find(dates[0])].strip()
        if "指數" not in name:
            continue
        rows.append({
            **meta,
            "index_name": re.sub(r"\s+", "", name),
            "announce_date": _iso(dates[0]),
            "effective_date": _iso(dates[1]),
        })
    return meta, rows


SCHEDULE_FIELDS = [
    "report_title", "update_date", "index_name", "announce_date", "effective_date",
]


if __name__ == "__main__":
    import sys
    meta, rows = parse_pdf(sys.argv[1])
    print("報告標題:", meta["report_title"])
    print("更新日期:", meta["update_date"])
    print("解析筆數:", len(rows))
    for r in rows:
        print(" ", r["index_name"], r["announce_date"], r["effective_date"])
