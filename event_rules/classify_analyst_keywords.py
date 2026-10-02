#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分析師關鍵字 → 依供應鏈知識庫分類 (人工已填的不動)
  來源 1：~/investment_system/knowledge_base/supply_chains/*.md —— 關鍵字出現在哪一篇、同一行提到哪些公司
  來源 2：題材概念股.csv —— 關鍵字出現在哪些 (子)題材名稱或角色欄
  輸出欄位：歸入題材、相關台股 (代號|分隔)、分類依據 (哪個檔/題材、命中幾行)
"""
import glob
import re
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

MAP = Path("/mnt/d/mops/news/mapping")
KB = "/home/tearicee/investment_system/knowledge_base/supply_chains/*.md"
INFO = "/mnt/d/finmind_data/TaiwanStockInfo/TaiwanStockInfo.parquet"
FILE_THEME = {"AI_ASIC與資料中心晶片生態鏈": "ASIC", "AI伺服器管理晶片": "AI伺服器-BMC", "HDD與AI儲存供應鏈": "HDD",
              "PCB載板設備與材料鏈": "PCB", "低軌衛星通訊": "低軌", "先進封裝生態鏈": "CoWoS/先進封裝",
              "化合物半導體功率元件": "第三代半導體", "半導體建廠CAPEX循環": "半導體建廠/廠務", "成熟製程與記憶體供需": "成熟製程",
              "成熟記憶體結構性缺貨": "記憶體", "無人機與國防": "無人機", "矽光與光通訊": "矽光子", "硬體安全與PQC": "資安硬體"}
C_CLS, C_STK, C_TH = "我的分類(公司/產品/技術/材料/忽略)", "相關台股(代號|分隔)", "歸入題材"


def kw_re(k: str):
    if re.fullmatch(r"[A-Za-z0-9 .\-/+]+", k):
        return re.compile(rf"(?<![A-Za-z0-9]){re.escape(k)}(?![A-Za-z0-9])", re.I)
    return re.compile(re.escape(k))


def main() -> int:
    info = pd.read_parquet(INFO).sort_values("date").drop_duplicates("stock_id", keep="last")
    names = dict(zip(info["stock_id"], info["stock_name"].str.replace(r"[*]|-KY", "", regex=True)))
    kb = {Path(f).stem: open(f, encoding="utf-8").read().split("\n") for f in glob.glob(KB)}
    t = pd.read_csv(MAP / "題材概念股.csv", dtype=str, keep_default_na=False)
    role_col = "角色(上游/中游/下游/設備/材料)"
    for path in MAP.glob("分析師關鍵字_*.csv"):
        k = pd.read_csv(path, dtype=str, keep_default_na=False)
        if "分類依據" not in k:
            k["分類依據"] = ""
        n_new = 0
        for i, term in enumerate(k["關鍵字"]):
            if k.at[i, C_CLS] == "忽略" or len(term) < 2:
                continue
            rx = kw_re(term)
            hits, codes = Counter(), Counter()
            for stem, lines in kb.items():
                for line in lines:
                    if rx.search(line):
                        hits[stem] += 1
                        for name, code in re.findall(r"([一-鿿A-Za-z\-\*]{2,10})\s*[（(]\s*(\d{4})\s*[）)]", line):
                            if code in names and names[code] in name:   # 名稱需對得上，避免把年份當代號
                                codes[code] += 1
            th_rows = t[t["題材"].str.contains(rx) | t[role_col].str.contains(rx)]
            if not hits and th_rows.empty:
                continue
            basis = []
            theme = ""
            if hits:
                stem, n = hits.most_common(1)[0]
                theme = FILE_THEME.get(stem, stem)
                basis.append(f"知識庫《{stem}》{n} 行")
            if not th_rows.empty:
                top = th_rows["題材"].value_counts()
                if not theme:
                    theme = top.index[0]
                basis.append(f"題材概念股「{top.index[0]}」等 {len(top)} 個題材")
                for c in th_rows["股票代號"]:
                    if c:
                        codes[c] += 1
            if not k.at[i, C_TH]:
                k.at[i, C_TH] = theme
                n_new += 1
            if not k.at[i, C_STK] or k.at[i, "分類依據"] == "":
                k.at[i, C_STK] = "|".join(c for c, _ in codes.most_common(12))
            if not k.at[i, C_CLS]:
                k.at[i, C_CLS] = "公司" if k.at[i, "台股代號"] else ("材料" if re.search("材料|樹脂|CCL|PTFE|膜|液", term) else "技術/產品")
            k.at[i, "分類依據"] = "；".join(basis)
        k.to_csv(path, index=False, encoding="utf-8-sig")
        print(path.name, len(k), "有題材", (k[C_TH] != "").sum(), "有依據", (k["分類依據"] != "").sum(), "新歸題材", n_new,
              "| 仍未分類", ((k[C_CLS] == "") & (k[C_TH] == "")).sum())
        print(k[C_CLS].replace("", "(未分類)").value_counts().to_dict())
    return 0


if __name__ == "__main__":
    sys.exit(main())
