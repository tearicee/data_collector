#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
概念股對照表產生器 (D:/mops/news/mapping/)
  1. 熱門族群.dsl (XQ 自選股匯出，cp950「分類:,2330.TW,…」) → 題材概念股.csv
     - DSL 每個分類一個題材 (來源=DSL，保留=Y)；同一檔可出現在多個題材
     - 另加 EXTRA_THEMES 跨股票題材：有新聞統計者列出系統建議 (來源=系統建議，保留留白)，
       沒有的留一列空白待填
     - 已存在的草稿若有人工填寫 (關聯強度/角色/備註/保留) 會保留
  2. 題材關鍵字.csv：每個題材對應的新聞關鍵字 (| 分隔)，供之後用新聞文字判斷題材
  3. 對象概念股：保留使用者已填列、補股票名稱，並依新聞共現統計補上各對象的台股 (來源=系統建議)
用法：python event_rules/build_mapping.py
"""
import re
import sys
import glob
import json
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

MAP_DIR = Path("/mnt/d/mops/news/mapping")
DSL = MAP_DIR / "熱門族群.dsl"
THEME_CSV = MAP_DIR / "題材概念股.csv"
KEYWORD_CSV = MAP_DIR / "題材關鍵字.csv"
ENTITY_CSV = MAP_DIR / "對象概念股.csv"
STOCK_INFO = "/mnt/d/finmind_data/TaiwanStockInfo/TaiwanStockInfo.parquet"
NEWS_DATA = "/mnt/d/mops/news/data/新聞_*.parquet"
NEWS_TAGGED = "/mnt/d/mops/news/tagged/新聞標籤_*.parquet"
THEME_COLS = ["題材", "股票代號", "股票名稱", "產業", "關聯強度(1-3)", "角色(上游/中游/下游/設備/材料)", "備註",
              "系統統計_近一年共現次數", "保留(Y/N)", "來源"]

# 跨股票題材 (DSL 沒有的)：題材 → 新聞關鍵字
EXTRA_THEMES = {
    "CoWoS/先進封裝": "CoWoS|CoPoS|先進封裝|SoIC|異質整合|3D封裝", "HBM": "HBM|高頻寬記憶體",
    "ASIC": "ASIC|客製化晶片|XPU", "矽光子": "矽光子|共同封裝光學|CPO", "液冷": "液冷|水冷|浸沒式|CDU",
    "BBU/電源": "BBU|備援電池|HVDC|高壓直流|電源供應器", "AI伺服器ODM": "AI伺服器|AI 伺服器|機櫃|GB200|GB300",
    "資料中心/AIDC": "資料中心|AIDC|算力中心|AI工廠", "人形機器人": "人形機器人|Optimus|機器人",
    "無人機": "無人機", "核能": "核能|核電|SMR|核融合", "氫能/燃料電池": "氫能|燃料電池|SOFC|Bloom",
    "第三代半導體": "碳化矽|SiC|氮化鎵|GaN|第三代半導體", "光罩/EUV": "光罩|EUV|護膜",
    "特用化學/先進製程耗材": "特用化學|特化|前驅物|研磨液|光阻", "探針卡/測試介面": "探針卡|測試座|測試介面",
    "CCL/銅箔基板": "CCL|銅箔基板|高階板材|M9|低介電", "玻纖布/銅箔": "玻纖布|石英布|電子級玻纖|銅箔",
    "AI手機": "AI手機|摺疊機|折疊機|iPhone", "MicroLED": "Micro LED|MicroLED", "電動車": "電動車|EV|充電",
    "自駕/Robotaxi": "自駕|Robotaxi|Cybercab|FSD", "太空/衛星地面設備": "SpaceX|Starlink|星鏈|地面站|火箭",
    "蘋果供應鏈": "蘋果|Apple|iPhone|iPad|Mac", "輝達供應鏈": "輝達|NVIDIA|Rubin|Blackwell|GTC",
    "美國設廠/亞利桑那": "亞利桑那|鳳凰城|美國設廠|德州設廠", "關稅受惠/受害": "關稅|232條款|對等關稅",
    "漲價概念": "漲價|調漲|報價上漲|喊漲", "減重藥/GLP-1": "減重|減肥藥|GLP-1|瘦瘦針", "CDMO": "CDMO|委託開發製造",
    "醫美": "醫美|玻尿酸|肉毒", "穩定幣/加密貨幣": "穩定幣|比特幣|加密貨幣|以太幣", "稀土/關鍵礦物": "稀土|關鍵礦物|鎢|鎵|鍺",
    "銅/原物料": "銅價|鋁價|原物料", "鎳/不銹鋼": "鎳價|不銹鋼", "電商/零售": "電商|雙11|零售|百貨",
    "高股息/ETF": "高股息|ETF換股|成分股", "中國內需/陸客": "陸客|中國內需|十一長假", "老AI/成熟製程": "成熟製程|8吋|驅動IC",
    "半導體建廠/廠務": "建廠|廠務|無塵室|配管|氣體供應", "電網/強韌電網": "電網|變壓器|配電|強韌電網",
    "傳產切入半導體/AI": "切入半導體|跨足半導體|搶進半導體|切入AI|跨足AI",
}


def stock_info() -> pd.DataFrame:
    d = pd.read_parquet(STOCK_INFO).sort_values("date").drop_duplicates("stock_id", keep="last")
    return d.set_index("stock_id")


DSL_RENAME = {"嗑曜集團": "上曜集團"}   # cp950 解碼時首字位元組錯位


def parse_dsl() -> list:
    t = DSL.read_bytes().decode("cp950", "replace")
    out = []
    for name, _, codes in re.findall(r"([^\s,:;\x00-\x1f\ufffd]{1,20})(:{1,3}),((?:\d{4,6}[A-Z]?\.T[WE],)+)", t):
        name = DSL_RENAME.get(name, name)
        out.append((name, [c.split(".")[0] for c in codes.strip(",").split(",")]))
    return out


def theme_cooccurrence(keywords: dict) -> dict:
    """題材關鍵字出現在標題的新聞裡，台股代號 (≤4 檔的文章) 近一年出現次數。"""
    tg = pd.concat(pd.read_parquet(f, columns=["發布時間", "標題", "個股代號"]) for f in sorted(glob.glob(NEWS_TAGGED)))
    tg = tg[tg["發布時間"] >= tg["發布時間"].max() - pd.Timedelta(days=365)]
    codes = tg["個股代號"].map(lambda s: [c for c in str(s).split(",") if c[:4].isdigit() and not c.startswith("00")])
    out = {}
    for th, kw in keywords.items():
        m = tg["標題"].str.contains(kw, regex=True, na=False)
        c = Counter(x for lst in codes[m] if 0 < len(lst) <= 4 for x in lst)
        out[th] = c
    return out


def build_themes(info: pd.DataFrame) -> pd.DataFrame:
    groups = parse_dsl()
    keywords = {name: re.escape(name) for name, _ in groups}
    keywords.update(EXTRA_THEMES)
    co = theme_cooccurrence(keywords)
    rows = []

    def row(th, code, src, keep):
        nm = info.at[code, "stock_name"] if code in info.index else ""
        ind = info.at[code, "industry_category"] if code in info.index else ""
        return dict(zip(THEME_COLS, [th, code, nm, ind, "", "", "", co.get(th, {}).get(code, 0) or "", keep, src]))

    dsl_members = defaultdict(set)
    for name, codes in groups:
        for c in codes:
            rows.append(row(name, c, "DSL", "Y"))
            dsl_members[name].add(c)
    for th in EXTRA_THEMES:
        sugg = [c for c, n in co[th].most_common(15) if n >= 3 and c in info.index]
        if not sugg:
            rows.append(dict(zip(THEME_COLS, [th, "", "", "", "", "", "待填", "", "", "新增題材"])))
        for c in sugg:
            rows.append(row(th, c, "系統建議", ""))
    new = pd.DataFrame(rows, columns=THEME_COLS)

    # 保留人工已填的欄位
    if THEME_CSV.exists():
        old = pd.read_csv(THEME_CSV, dtype=str, keep_default_na=False)
        manual = [c for c in ("關聯強度(1-3)", "角色(上游/中游/下游/設備/材料)", "備註", "保留(Y/N)") if c in old]
        old = old[(old[manual] != "").any(axis=1)] if manual else old.iloc[0:0]
        key = lambda d: d["題材"] + "|" + d["股票代號"].astype(str)
        keep = old.set_index(key(old))
        for c in manual:
            m = key(new).map(keep[c].to_dict()).fillna("")
            new[c] = m.where(m != "", new[c])
        extra = old[~key(old).isin(set(key(new)))]
        if len(extra):
            extra = extra.reindex(columns=THEME_COLS).fillna("")
            extra["來源"] = extra["來源"].replace("", "使用者")
            new = pd.concat([new, extra], ignore_index=True)
    new.to_csv(THEME_CSV, index=False, encoding="utf-8-sig")
    kw = pd.DataFrame({"題材": list(keywords), "新聞關鍵字(|分隔)": [k.replace("\\", "") for k in keywords.values()],
                       "來源": ["DSL" if k not in EXTRA_THEMES else "新增題材" for k in keywords]})
    if not KEYWORD_CSV.exists():
        kw.to_csv(KEYWORD_CSV, index=False, encoding="utf-8-sig")
    return new


# 美股/國際大廠：對象 → 別名 (公司名、代號、代表人物)
ENTITY_ALIASES = {
    "輝達": "NVIDIA|Nvidia|NVDA|黃仁勳", "蘋果": "Apple|AAPL|庫克|iPhone", "微軟": "Microsoft|MSFT|納德拉|Azure",
    "亞馬遜": "Amazon|AMZN|AWS", "谷歌": "Google|Alphabet|GOOGL|GOOG|Gemini|TPU", "Meta": "Meta|臉書|祖克柏",
    "特斯拉": "Tesla|TSLA|馬斯克", "超微": "AMD|蘇姿丰", "英特爾": "Intel|INTC", "博通": "Broadcom|AVGO",
    "高通": "Qualcomm|QCOM", "美光": "Micron|MU", "邁威爾": "Marvell|MRVL", "戴爾": "Dell", "慧與": "HPE|Hewlett Packard",
    "美超微": "Supermicro|SMCI|超微電腦", "甲骨文": "Oracle|ORCL", "OpenAI": "OpenAI|ChatGPT|奧特曼",
    "Anthropic": "Anthropic|Claude", "SpaceX": "SpaceX|Starlink|星鏈", "思科": "Cisco|CSCO", "Arista": "Arista|ANET",
    "Lumentum": "魯門特姆|LITE|Lumentum", "Coherent": "Coherent|COHR", "Bloom Energy": "Bloom Energy|BE",
    "Vertiv": "Vertiv|維諦|VRT", "伊頓": "Eaton|ETN", "GE Vernova": "GE Vernova|GEV", "應材": "Applied Materials|應用材料|AMAT",
    "科林研發": "Lam Research|LRCX", "科磊": "KLA|KLAC", "艾司摩爾": "ASML|艾司摩爾", "德儀": "Texas Instruments|德州儀器|TI",
    "威騰": "Western Digital|WDC|威騰電子", "希捷": "Seagate|STX", "SanDisk": "SanDisk|SNDK|晟碟",
    "Palantir": "Palantir|PLTR", "CoreWeave": "CoreWeave|CRWV", "波音": "Boeing|BA", "禮來": "Eli Lilly|LLY|禮來",
    "諾和諾德": "Novo Nordisk|諾和諾德", "三星": "Samsung|三星電子", "SK海力士": "SK Hynix|SK海力士|海力士",
    "軟銀": "SoftBank|軟銀|孫正義", "索尼": "Sony|索尼", "任天堂": "Nintendo|任天堂", "豐田": "Toyota|豐田",
    "華為": "華為|Huawei", "比亞迪": "比亞迪|BYD", "小米": "小米|Xiaomi", "安謀": "Arm|安謀",
    "Credo": "Credo|CRDO", "Astera Labs": "Astera|ALAB", "安費諾": "Amphenol|安費諾|APH", "康寧": "Corning|康寧|GLW",
}
ENTITY_COLS = ["對象", "別名(|分隔)", "關係類型", "股票代號", "股票名稱", "關聯強度(1-3)", "角色", "題材",
               "系統統計_近一年共現次數", "保留(Y/N)", "來源"]


def build_entities(info: pd.DataFrame) -> pd.DataFrame:
    user = pd.read_csv(ENTITY_CSV, dtype=str, keep_default_na=False) if ENTITY_CSV.exists() else pd.DataFrame()
    if len(user) and "來源" in user:
        user = user[user["來源"].isin(["使用者", ""])]
    aliases = dict(ENTITY_ALIASES)
    for ent, al in zip(user.get("對象", []), user.get("別名(|分隔)", [])):
        aliases[ent] = al or aliases.get(ent, ent)
    tg = pd.concat(pd.read_parquet(f, columns=["發布時間", "標題", "個股代號"]) for f in sorted(glob.glob(NEWS_TAGGED)))
    tg = tg[tg["發布時間"] >= tg["發布時間"].max() - pd.Timedelta(days=365)]
    codes = tg["個股代號"].map(lambda s: [c for c in str(s).split(",") if c[:4].isdigit() and not c.startswith("00")])
    rows = []
    for _, r in user.iterrows():
        d = {c: r.get(c, "") for c in ENTITY_COLS}
        c = str(d["股票代號"])
        if not d["股票名稱"] and c in info.index:
            d["股票名稱"] = info.at[c, "stock_name"]
        d["保留(Y/N)"], d["來源"] = d["保留(Y/N)"] or "Y", "使用者"
        rows.append(d)
    have = {(d["對象"], str(d["股票代號"])) for d in rows}
    for ent, al in aliases.items():
        names = [ent] + [a for a in al.split("|") if a]
        pat = "|".join(re.escape(n) if not re.fullmatch(r"[A-Za-z]{1,4}", n) else rf"(?<![A-Za-z]){n}(?![A-Za-z])"
                       for n in names)
        m = tg["標題"].str.contains(pat, regex=True, na=False)
        cnt = Counter(x for lst in codes[m] if 0 < len(lst) <= 4 for x in lst)
        sugg = [(c, n) for c, n in cnt.most_common(15) if n >= 3 and c in info.index and (ent, c) not in have]
        for d in rows:
            if d["對象"] == ent and not d["系統統計_近一年共現次數"]:
                d["系統統計_近一年共現次數"] = cnt.get(str(d["股票代號"]), "") or ""
        if not sugg and not any(d["對象"] == ent for d in rows):
            rows.append(dict(zip(ENTITY_COLS, [ent, al, "", "", "", "", "", "", "", "", "待填"])))
        for c, n in sugg:
            rows.append(dict(zip(ENTITY_COLS, [ent, al, "供應鏈", c, info.at[c, "stock_name"], "", "", "", n, "", "系統建議"])))
    out = pd.DataFrame(rows, columns=ENTITY_COLS)
    out.to_csv(ENTITY_CSV, index=False, encoding="utf-8-sig")
    return out


if __name__ == "__main__":
    info = stock_info()
    t = build_themes(info)
    print(f"題材概念股：{t['題材'].nunique()} 個題材、{len(t)} 列；來源 {t['來源'].value_counts().to_dict()}")
    e = build_entities(info)
    print(f"對象概念股：{e['對象'].nunique()} 個對象、{len(e)} 列；來源 {e['來源'].value_counts().to_dict()}")
