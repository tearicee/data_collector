"""
解析台灣指數公司「成分股審核結果」PDF，輸出為單一 CSV。

輸出欄位：
- index_name     指數名稱
- announce_date  公告日期 (YYYY-MM-DD)
- review_date    定審資料日 (YYYY-MM-DD)；無則留空
- effective_date 生效日 (YYYY-MM-DD)；無則留空
- action         納入 / 刪除
- stock_id       成分股代號
- stock_name     成分股名稱
"""

import os
import re
import sys
import csv

import pdfplumber


def _roc_or_ad_to_iso(text: str) -> str | None:
    """從文字中擷取西元年月日並轉成 YYYY-MM-DD。

    支援「2026年6月26日」「2026/06/26」「(20260609)」等格式。
    """
    m = re.search(r"(20\d{2})\s*[年/]\s*(\d{1,2})\s*[月/]\s*(\d{1,2})", text)
    if m:
        y, mo, d = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", text)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{mo}-{d}"
    return None


# 成分股代號：4~6 碼數字（台股 4 碼、韓股 6 碼）可帶單一字母尾碼（如特別股 1101B）。
# 尾碼字母僅在其後「非拉丁字母」時才納入，避免外文名(如 3673 TPK-KY、6456 GIS-KY)
# 去空白後黏成 3673T / 6456G 而誤判代號。
_CODE_RE = re.compile(r"\d{4,6}(?:[A-Z](?![A-Za-z]))?")


def _parse_section(body: str, keyword: str) -> list[tuple[str, str]]:
    """擷取「<keyword>(N)：...」段落內所有 (代號, 名稱)。

    段落到下一個「成分股」關鍵字、「*」「如欲」或文末為止。
    以「代號」為邊界切分（名稱 = 兩代號之間的文字），可同時處理：
    - 頓號「、」或換行分隔（臺韓資訊科技指數用換行分隔）
    - 名稱跨行被切斷（如「瑞\n昱」→ 去除中文間空白還原）
    - 中／英文名稱（韓股英文名保留字間空白）
    """
    m = re.search(keyword + r"\s*[（(]\s*\d+\s*[)）]\s*[:：]", body)
    if not m:
        return []
    rest = body[m.end():]
    # 段落終止：下一個中文成分股關鍵字 / 註記 / 英文附錄(舊 PDF 為中英雙語，
    # 中文刪除段後接 Review Result、Inclusions(N)、Exclusions(N) 等英文清單，
    # 若不在此截斷會把英文代號一併吞入造成同股既納入又刪除)。
    end = re.search(
        r"成分股(?:納入|刪除|不變)|^\s*\*|如欲取得|"
        r"Review\s+Result|Inclusions\s*[（(]|Exclusions\s*[（(]",
        rest, re.M,
    )
    segment = rest[: end.start()] if end else rest
    # 移除子標題（臺灣／韓國證券交易所掛牌股票：）避免被當成名稱
    segment = re.sub(r"[^\n、]*掛牌股票\s*[:：]", "\n", segment)

    codes = list(_CODE_RE.finditer(segment))
    out = []
    for i, cm in enumerate(codes):
        s = cm.end()
        e = codes[i + 1].start() if i + 1 < len(codes) else len(segment)
        name = segment[s:e]
        name = re.sub(r"^[\s、，,]+", "", name)              # 去開頭分隔符
        name = re.sub(r"\s+", " ", name).strip(" 、，,\n")    # 收斂空白
        name = re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "", name)  # 去中文跨行造成的空白
        if name:
            out.append((cm.group(0), name))
    return out


def parse_pdf(pdf_path: str) -> tuple[dict, list[dict]]:
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)

    # 指數名稱：標題第一個「...指數」（標題可能跨行被切斷，先去換行）
    name_m = re.search(r"「\s*(.*?指數)\s*」", text.replace("\n", ""))
    index_name = re.sub(r"\s+", "", name_m.group(1)) if name_m else ""

    # 公告日期 = 文中第一個日期（標題下方那行）
    announce_date = _roc_or_ad_to_iso(text)

    # 生效日 = 「...起生效」前最接近的日期（日期內可能夾空白，故用容忍空白的 _iso）
    eff_m = re.search(r"(20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)[^年。]*?起生效", text)
    effective_date = _roc_or_ad_to_iso(eff_m.group(1)) if eff_m else None

    rev_m = re.search(r"定審資料日\s*[（(]\s*(\d{8})\s*[)）]", text)
    review_date = _roc_or_ad_to_iso(rev_m.group(1)) if rev_m else None

    meta = {
        "index_name": index_name,
        "announce_date": announce_date or "",
        "review_date": review_date or "",
        "effective_date": effective_date or "",
    }

    # 中文段為主；舊版中英雙語 PDF 另以英文 Inclusions/Exclusions 段補救
    # (中文段偶有數字擷取掉位，如「5871 中租-KY」被擷成「587 中租」而漏抓)。
    rows = []
    for action, cn_kw, en_kw in (("納入", "成分股納入", "Inclusions"),
                                 ("刪除", "成分股刪除", "Exclusions")):
        seen, items = set(), []
        for kw in (cn_kw, en_kw):
            for sid, sname in _parse_section(text, kw):
                if sid not in seen:
                    seen.add(sid)
                    items.append((sid, sname))
        for sid, sname in items:
            rows.append({**meta, "action": action, "stock_id": sid, "stock_name": sname})
    return meta, rows


FIELDS = [
    "index_name", "announce_date", "review_date", "effective_date",
    "action", "stock_id", "stock_name",
]


def write_csv(rows: list[dict], out_path: str, append: bool = False):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    new_file = not (append and os.path.exists(out_path))
    mode = "a" if append and not new_file else "w"
    with open(out_path, mode, encoding="utf-8_sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    pdf_path = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else "/mnt/d/taiwanindex/technical_notice/constituents.csv"
    meta, rows = parse_pdf(pdf_path)
    print("指數名稱  :", meta["index_name"])
    print("公告日期  :", meta["announce_date"])
    print("定審資料日:", meta["review_date"])
    print("生效日    :", meta["effective_date"])
    print("解析筆數  :", len(rows),
          f"(納入 {sum(r['action']=='納入' for r in rows)} / 刪除 {sum(r['action']=='刪除' for r in rows)})")
    write_csv(rows, out_path, append="--append" in sys.argv)
    print("已寫入    :", out_path)
