#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
財報狗產業報告 PDF → 各製程環節的台灣上市櫃/興櫃業者
  報告格式：「<環節名稱>[:說明]」之後接「海外業者:…」「台灣業者/本土業者/主要業者/代工與生產:…」(可能換行)。
  輸出每列 = (主題, 類別[材料/設備], 製程階段, 環節, 股票代號, 名稱, 原文, 註記)。
  - 沒寫代號的業者用股票簡稱比對 (光洋科、鑫科、昶昕實業…)；比對不到的 (未上市) 另存 unlisted。
  - 報告的代號與名稱對不上時：名稱對得到別檔股票 → 以名稱為準並註記；否則保留代號並註記「請確認」。
  - 「添鴻科技 (弘塑 3131 子公司)」這類 → 記在上市母公司，註記透過子公司/轉投資。
用法：python event_rules/parse_statementdog_pdf.py   (結果寫到 mapping/_history/pdf_parsed_steps.csv)
"""
import glob
import re
import unicodedata

import pandas as pd
from pypdf import PdfReader

STOCK_INFO = "/mnt/d/finmind_data/TaiwanStockInfo/TaiwanStockInfo.parquet"
PDF_GLOB = "/home/tearicee/investment_system/_ARCHIVE/2026-0[47]/*.pdf"
OUT_DIR = "/mnt/d/mops/news/mapping/_history/"
CJK = "一-鿿"
OVERSEAS_LABELS = ("海外業者", "規格 / 配方主導者", "Stepper", "LDI")
TW_LABELS = ("台灣業者", "本土業者", "主要業者", "代工與生產", "TGV 加工業者", "玻璃載板業者", "玻璃核心供應",
             "面板製造", "玻璃載板", "晶圓製造業者", "封裝業者", "載板業者")
LABEL_AS_HEADER = ("TGV 加工業者", "玻璃載板業者", "玻璃核心供應", "面板製造", "玻璃載板", "晶圓製造業者", "封裝業者", "載板業者")
VENDOR_RE = re.compile(r"^\s*(" + "|".join(re.escape(x).replace(r"\ ", r"\s*") for x in OVERSEAS_LABELS + TW_LABELS)
                       + r")\s*[:：]")
HEADER_KW = re.compile(r"設備|材料|液|劑|膜|靶材|光阻|乾膜|樹脂|銅柱|銅鹽|藥水|載具|氣體|化學品|Carrier|Molding|Agents|機$|檢測|量測|研磨墊|模組")
SKIP = re.compile(r"圖片來源|下面我們|彙整|^\(|^表|^圖")
ALIAS = {"永光化學": "永光", "昶昕實業": "昶昕", "達興": "達興材料", "晶呈科": "晶呈科技"}
NOT_COMPANY = {"材料", "通訊", "三星", "世界", "大陸", "聯合", "中華", "台灣", "國產", "統一", "第一", "全新", "精華"}
MAX_CONT = 2   # 業者清單最多接續幾行


def load_text(path: str) -> list:
    text = "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
    text = unicodedata.normalize("NFKC", text).replace("⻑", "長")
    text = re.sub(r"\d{4}/\d{1,2}/\d{1,2} [上下]午 \d{1,2}:\d{2}[^\n]*\n?", "", text)
    text = re.sub(r"https://statementdog\.com\S*\s*\d+/\d+", "", text)
    return [re.sub(r"\s+", " ", l).strip() for l in text.split("\n") if l.strip()]


def _base(n: str) -> str:
    return re.sub(r"[*]|-KY|-創$|創$|科技$|實業$", "", n or "")


def names_map() -> tuple:
    """回傳 (簡稱→代號, 代號→簡稱, 代號→市場別)。同代號上市櫃優先於興櫃。"""
    d = pd.read_parquet(STOCK_INFO)
    d = d[d["stock_id"].str.fullmatch(r"[1-9]\d{3}") & d["type"].isin(["twse", "tpex", "emerging"])].copy()
    d["rank"] = d["type"].map({"twse": 0, "tpex": 0, "emerging": 1})
    d = d.sort_values(["rank", "date"], ascending=[True, False]).drop_duplicates("stock_id", keep="first")
    n2c = dict(zip(d["stock_name"].str.replace(r"[*]|-KY$|-創$|創$", "", regex=True), d["stock_id"]))
    return n2c, dict(zip(d["stock_id"], d["stock_name"])), dict(zip(d["stock_id"], d["type"]))


def parse(path: str, topic: str):
    lines = load_text(path)
    names, code_name, code_type = names_map()
    rows, unlisted = [], []
    st = {"kind": "", "stage": "", "header": "", "label": "", "buf": None, "cont": 0, "in_desc": False}

    def flush():
        buf, st["buf"] = st["buf"], None
        if buf is None or not any(st["label"].replace(" ", "") == x.replace(" ", "") for x in TW_LABELS):
            return
        for tok in re.split(r"[、,，]", buf):
            tok = tok.strip()
            if not tok:
                continue
            m = re.search(r"(\d{4})", tok)
            nm = re.sub(r"[\(（].*", "", tok).strip()
            nm = re.sub(r"\s*[/+].*", "", nm).strip()
            key = ALIAS.get(nm, nm)
            by_name = names.get(key, "") if (key not in NOT_COMPANY and 2 <= len(key) <= 6) else ""
            code, note = "", ""
            if m and m.group(1) in code_name:
                code = m.group(1)
                listed = code_name[code]
                if re.search(r"子公司|轉投資", tok) or re.search(rf"[\(（]\s*[{CJK}]{{2,6}}\s*\d{{4}}", tok):
                    note = f"透過子公司/轉投資：{nm}"
                elif re.search(rf"[{CJK}]", nm) and _base(nm) not in _base(listed) and _base(listed) not in _base(nm):
                    if by_name and by_name != code:      # 報告代號誤植，名稱對得到別檔 → 以名稱為準
                        note, code = f"報告寫 {nm} ({code})，依名稱改為 {by_name}", by_name
                    else:
                        note = f"報告寫「{nm}」但 {code} 是「{listed}」，請確認"
            elif by_name:
                code = by_name
            if not code:
                if re.search(rf"[{CJK}]", nm):
                    unlisted.append({"主題": topic, "環節": st["header"], "業者": nm})
                continue
            if code_type.get(code) == "emerging":
                note = (note + "；" if note else "") + "興櫃"
            rows.append({"主題": topic, "類別": st["kind"], "製程階段": st["stage"], "環節": st["header"],
                         "股票代號": code, "名稱": code_name.get(code, nm), "原文": tok, "註記": note})

    for i, line in enumerate(lines):
        if "供應鏈地圖" in line and len(line) < 16:
            flush()
            st["kind"] = "材料" if "材料" in line else ("設備" if "設備" in line else st["kind"])
            continue
        if re.search(r"供應鏈$", line) and len(line) < 24 and ":" not in line:
            flush()
            st["stage"] = re.sub(r"製程與供應鏈$|與供應鏈$|供應鏈$", "", line).strip()
            continue
        m = VENDOR_RE.match(line)
        if m:
            flush()
            st["label"] = re.sub(r"\s+", " ", m.group(1)).strip()
            st["buf"], st["cont"], st["in_desc"] = line[m.end():], 0, False
            if st["label"] in LABEL_AS_HEADER:
                st["header"] = st["label"].replace("業者", "")
            continue
        has_code = re.search(r"\(\d{4}\)", line)
        colon_form = re.match(r"^[^:：。,，]{2,46}[:：]", line)
        next_is_vendor = i + 1 < len(lines) and VENDOR_RE.match(lines[i + 1]) is not None
        kw_form = (HEADER_KW.search(line) and 4 <= len(line) < 30 and not re.search(r"[。,，]", line)
                   and (not st["in_desc"] or next_is_vendor))
        is_header = (not SKIP.search(line)) and len(line) < 70 and not has_code and (colon_form or kw_form)
        if is_header and not any(VENDOR_RE.match(x) for x in lines[i + 1:i + 5]):
            is_header = False   # 後面 4 行內沒有業者清單 → 只是一般段落
        if is_header:
            flush()
            st["header"] = re.split(r"[:：]", line)[0].strip()
            st["in_desc"] = bool(colon_form)   # 有冒號說明的才可能換行接續
            continue
        if st["buf"] is not None:
            listy = ("、" in line or has_code or len(line) <= 22) and "。" not in line
            if st["cont"] < MAX_CONT and listy:
                st["buf"] += line
                st["cont"] += 1
            else:
                flush()
    flush()
    cols = ["主題", "類別", "製程階段", "環節", "股票代號", "名稱", "原文", "註記"]
    df = pd.DataFrame(rows, columns=cols).drop_duplicates(["環節", "股票代號"])
    return df, pd.DataFrame(unlisted, columns=["主題", "環節", "業者"]).drop_duplicates()


def parse_all() -> tuple:
    out, un = [], []
    for f in sorted(glob.glob(PDF_GLOB)):
        topic = "FOPLP" if ("PLP" in f or "面板級" in f) else "玻璃基板"
        r, u = parse(f, topic)
        print(f.split("/")[-1][:34], "→", len(r), "列", r["股票代號"].nunique(), "檔；未上市業者", len(u))
        out.append(r)
        un.append(u)
    return pd.concat(out, ignore_index=True).drop_duplicates(["主題", "環節", "股票代號"]), pd.concat(un, ignore_index=True)


if __name__ == "__main__":
    df, un = parse_all()
    df.to_csv(OUT_DIR + "pdf_parsed_steps.csv", index=False, encoding="utf-8-sig")
    un.to_csv(OUT_DIR + "pdf_parsed_unlisted.csv", index=False, encoding="utf-8-sig")
    print(df.groupby("主題").agg(列=("股票代號", "count"), 檔=("股票代號", "nunique"), 環節=("環節", "nunique")).to_string())
