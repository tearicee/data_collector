"""無損修正 成分股調整紀錄.xlsx 的 4 處錯誤(直接改 sheet1.xml，保留樞紐表/樣式)。

修正內容(經官方 PDF 逐筆確認):
1. A1440 (00881 2023-10-19 納入)  2356 → 2352
2. A1540 (00919 2023-12-18 刪除)  2356 → 2352
3. 00918 2025-07-01 補 1504 刪除(原檔漏記) → 附加 1 列
4. 00713 2023-06-16 整批 40 列為錯誤資料 → 就地覆寫為官方 42 列(40 覆寫 + 2 附加)

做法:讀原 zip，僅替換 xl/worksheets/sheet1.xml，其餘條目原樣複製。
"""
import os, re, json, shutil, zipfile, datetime

D = "/mnt/d/taiwanindex/technical_notice"
SRC = os.path.join(D, "成分股調整紀錄.xlsx")
BAK = os.path.join(D, "成分股調整紀錄_備份_20260629.xlsx")
RAW = os.path.join(D, "追蹤ETF成分股調整_全史_raw.json")
EPOCH = datetime.date(1899, 12, 30)


def serial(d: str) -> int:
    return (datetime.date.fromisoformat(d) - EPOCH).days


def build_row(n, sid, rev, pub, eff, typ, etf, is_new=False):
    """組一個 A:G 列的 XML(字串欄用 inlineStr，日期用序號)。"""
    cells = [f'<c r="A{n}" s="1"><v>{sid}</v></c>']
    for col, d in (("B", rev), ("C", pub), ("D", eff)):
        if d:
            cells.append(f'<c r="{col}{n}" s="5"><v>{serial(d)}</v></c>')
    cells.append(f'<c r="E{n}" s="1" t="inlineStr"><is><t>{typ}</t></is></c>')
    cells.append(f'<c r="F{n}" s="3" t="inlineStr"><is><t>{etf}</t></is></c>')
    cells.append(f'<c r="G{n}" s="17" t="b"><v>{1 if is_new else 0}</v></c>')
    return f'<row r="{n}" spans="1:7">' + "".join(cells) + "</row>"


def main():
    assert not os.path.exists(BAK), "備份已存在，請先確認"
    shutil.copy2(SRC, BAK)
    print("已備份 →", BAK)

    web = json.load(open(RAW, encoding="utf-8"))
    cor = [r for r in web if r["etf"] == "00713" and r["public_date"] == "2023-06-16"]
    cor.sort(key=lambda r: (r["type"], r["stock_id"]))
    assert len(cor) == 42, len(cor)

    z = zipfile.ZipFile(SRC)
    xml = z.read("xl/worksheets/sheet1.xml").decode("utf-8")

    # --- 1 & 2: 錯字 ---
    for cell in ("A1440", "A1540"):
        old = f'<c r="{cell}" s="1"><v>2356</v></c>'
        new = f'<c r="{cell}" s="1"><v>2352</v></c>'
        assert xml.count(old) == 1, (cell, xml.count(old))
        xml = xml.replace(old, new)

    # --- 4: 00713 2023-06-16 覆寫列 1338..1377 ---
    rows_1338 = list(range(1338, 1378))  # 40 列
    for i, n in enumerate(rows_1338):
        c = cor[i]
        new_row = build_row(n, c["stock_id"], c["review_date"], c["public_date"],
                            c["effective_date"], c["type"], "00713")
        pat = re.compile(r'<row r="%d"[^>]*>.*?</row>' % n, re.S)
        assert pat.search(xml), f"row {n} 找不到"
        xml = pat.sub(lambda m: new_row, xml, count=1)

    # --- 附加列：00713 剩 2 列 + 00918 的 1504 ---
    appended = []
    nxt = 2524
    for c in cor[40:]:
        appended.append(build_row(nxt, c["stock_id"], c["review_date"], c["public_date"],
                                  c["effective_date"], c["type"], "00713"))
        nxt += 1
    # 3: 00918 1504 刪除
    appended.append(build_row(nxt, "1504", "2025-06-13", "2025-07-01", "2025-07-04",
                              "delete", "00918"))
    nxt += 1

    xml = xml.replace("</sheetData>", "".join(appended) + "</sheetData>")
    last = nxt - 1
    xml = re.sub(r'<dimension ref="A1:O\d+"/>', f'<dimension ref="A1:O{last}"/>', xml)

    # --- 重新打包 ---
    tmp = SRC + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in z.infolist():
            data = xml.encode("utf-8") if item.filename == "xl/worksheets/sheet1.xml" else z.read(item.filename)
            zout.writestr(item, data)
    z.close()
    os.replace(tmp, SRC)
    print(f"完成：覆寫 40 列、附加 {len(appended)} 列(2×00713 + 1×00918），新最後列 {last}")


if __name__ == "__main__":
    main()
