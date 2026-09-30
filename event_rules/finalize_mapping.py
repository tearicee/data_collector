#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
補完 mapping 的關聯 (系統先填，使用者之後查驗)：
  1. 題材關鍵字.csv   ：替 DSL 題材補上新聞常用同義詞 (SYNONYMS)
  2. 題材概念股.csv   ：系統建議列標 保留=Y、備註「系統建議，待查驗」，關聯強度依共現次數；DSL 列強度 3
  3. 對象概念股.csv   ：同上
  4. 分析師關鍵字_*.csv：依 TERM_RULES 填「我的分類 / 歸入題材」，相關台股取該題材的成員
人工已填的值一律不覆寫。
"""
import re
import sys
from pathlib import Path

import pandas as pd

MAP = Path("/mnt/d/mops/news/mapping")
NOTE = "系統建議，待查驗"

SYNONYMS = {  # DSL 題材 → 新聞常見用詞
    "太陽能": "太陽能|光伏|太陽光電", "儲能": "儲能|電池儲能|ESS", "重電": "重電|變壓器|電網|配電盤|強韌電網",
    "能源": "綠能|綠電|再生能源|能源轉型", "邊緣運算": "邊緣運算|邊緣AI|Edge AI|工業電腦|IPC", "光學": "光學鏡頭|鏡頭|光學元件",
    "機器人光學": "機器人視覺|機器視覺|3D感測", "機器人": "機器人|人形機器人|Optimus|機械手臂", "工具機": "工具機|CNC",
    "減速機": "減速機|諧波|行星減速", "封測材料": "探針卡|測試座|測試介面|封測材料", "面板": "面板|顯示器|LCD|OLED",
    "水資源": "水資源|水處理|再生水|缺水", "化工": "化工|特化|特用化學", "光通訊CPO大雜燴": "CPO|矽光子|光通訊|光收發|光模組|共同封裝光學",
    "InP": "InP|磷化銦|砷化鎵", "連接器": "連接器|高速連接", "連接線材": "連接線|線材|AEC|銅纜", "電子紙": "電子紙|E Ink",
    "金流": "金流|第三方支付|電子支付", "遊戲": "遊戲股|線上遊戲|手遊", "LED": "LED|Micro LED|Mini LED", "網通": "網通|交換器|路由器",
    "AOI": "AOI|光學檢測|檢測設備", "設備廠商指標": "半導體設備|設備廠", "FOPLP": "FOPLP|面板級封裝|扇出型面板", "設備2": "半導體設備|濕製程|雷射設備",
    "IC製造本土供應鏈": "在地供應鏈|本土供應鏈|特用化學|先進製程材料", "半導體設備": "半導體設備", "無塵室": "無塵室|廠務|建廠",
    "半導體氣體二配": "氣體供應|二次配|特殊氣體", "顯示相關IC": "驅動IC|TDDI|顯示IC", "IC製造": "晶圓代工|先進製程|2奈米|3奈米|成熟製程",
    "航運": "航運|貨櫃|運價|SCFI|散裝|BDI", "鋼鐵": "鋼鐵|鋼價|盤價|不銹鋼|鎳價", "PMIC": "PMIC|電源管理IC", "功率半導體": "功率半導體|MOSFET|IGBT|碳化矽|SiC|氮化鎵|GaN",
    "AIPC": "AI PC|AIPC|Copilot PC", "電纜": "電線電纜|電纜|銅價", "防疫": "防疫|疫情|新冠|流感|口罩", "散熱": "散熱|均熱片|熱管|水冷|液冷|CDU",
    "比特幣": "比特幣|加密貨幣|挖礦|礦機", "伺服器導軌": "伺服器導軌|滑軌", "AI": "AI伺服器|AI 伺服器|機櫃|GB200|GB300|伺服器代工",
    "車用組件": "汽車零組件|車用零件|AM件", "石英元件": "石英元件|石英晶體|振盪器", "樞紐": "樞紐|軸承|摺疊機|折疊機", "資安硬體": "資安|資訊安全",
    "軟體": "軟體|雲端服務|SaaS|系統整合", "生技": "生技|新藥|臨床|藥證", "手機": "手機|智慧型手機", "AWS": "AWS|亞馬遜雲端",
    "ABF": "ABF|載板|IC載板", "PCB": "PCB|銅箔基板|CCL|電路板|高階板材|M9", "PCB耗材": "鑽針|PCB耗材", "鑽孔": "鑽孔|鑽針|背鑽",
    "軟板": "軟板|FPC", "PCB設備": "PCB設備", "低軌": "低軌衛星|衛星|LEO|Starlink|星鏈", "檢測": "材料分析|故障分析|MA|FA|檢測實驗室",
    "碳權": "碳權|碳費|碳交易", "風電": "風電|離岸風電", "HDD": "硬碟|HDD", "記憶體": "記憶體|DRAM|NAND|NOR|快閃",
    "記憶體模組": "記憶體模組|SSD|控制晶片", "記憶體封測": "記憶體封測", "IC通路": "IC通路|半導體通路", "航空": "航空|航班|載客率",
    "旅遊": "旅遊|觀光|飯店|旅展", "飛機零件": "航太|飛機零件|引擎零件", "軍工": "軍工|國防|軍備|無人機|飛彈", "造船": "造船|國艦國造|潛艦",
    "餐飲": "餐飲|連鎖餐飲|展店", "汽車": "汽車|車市|新車", "充電樁": "充電樁|充電站", "資產": "資產股|土地開發|都更", "營建": "營建|建案|房市|預售",
    "肥料": "肥料|農糧", "金融": "金控|銀行|壽險|升息|降息", "證券": "證券|券商|成交量", "水泥": "水泥|預拌", "矽晶圓": "矽晶圓|晶圓片",
    "特特概念": "特斯拉|Tesla|馬斯克", "電池": "電池|鋰電|BBU|電池模組", "車用半導體": "車用半導體|導線架", "被動": "被動元件|MLCC|電容|電阻|電感",
    "power": "電源供應器|PSU|HVDC|電源", "塑化": "塑化|乙烯|石化", "AR眼鏡": "AR眼鏡|智慧眼鏡|MR|VR", "玻璃基板": "玻璃基板|玻璃載板|TGV|玻璃芯",
    "遊戲機": "遊戲機|Switch|PS5", "安控": "安控|監視器|影像監控", "封測": "封測|封裝測試", "量子電腦": "量子電腦|量子",
    "紡織": "紡織|成衣|機能布", "spaceX": "SpaceX|星艦", "廢棄物回收": "廢棄物|回收|循環經濟", "貴金屬": "黃金|白銀|貴金屬|金價|銅價",
    "MCU": "MCU|微控制器", "發哥集團": "聯發科|發哥", "神盾": "神盾集團", "鴻海": "鴻海集團|鴻家軍", "大同": "大同集團",
    "東元集團": "東元集團", "晟德集團": "晟德集團", "家登集團": "家登|光罩盒|載具", "SMCI": "美超微|Supermicro|SMCI", "甲骨文": "甲骨文|Oracle",
}

# 分析師關鍵字 → (分類, 歸入題材)
TERM_RULES = [
    (r"^(CCL|PTFE|HC|PP|M9|M10|M9-class|M10-class|SG\d+N?|SIF\d+|HC PP|HC CCL|PTFE CCL|.*-based (CCL|PP)|無布.*)$", "材料", "CCL/銅箔基板"),
    (r"PCB|switch tray|Kyber|背板|Inter-tray", "產品", "PCB"),
    (r"Rubin|NVL\d+|Vera|CPX|Blackwell|GB[23]00|NVLink|Spectrum|MGX|^Nvidia$|^NVIDIA$|GTC", "產品", "輝達供應鏈"),
    (r"CoPoS|CoWoS|^CoP$|InFO|SoIC|先進封裝|WMCM", "技術", "CoWoS/先進封裝"),
    (r"FOPLP|面板級", "技術", "FOPLP"), (r"GCS|glass|玻璃", "技術", "玻璃基板"),
    (r"EUV|High-NA|^ASML$|Carl Zeiss", "技術", "光罩/EUV"),
    (r"^HBM\d*|HBM", "產品", "HBM"), (r"DRAM|GDDR\d|NAND|LPDDR", "產品", "記憶體"),
    (r"^Apple$|iPhone|iPad|MacBook|Vision Pro|AirPods|WWDC|Apple Watch|^A\d{2}$|Siri", "產品", "蘋果供應鏈"),
    (r"^TSMC$|台積電|2nm|3nm|N2|A16|晶圓代工", "公司", "IC製造"),
    (r"OSFP|Ethernet|RDMA|CPO|矽光|光纖|光模組|Scale-(up|out)", "技術", "矽光子"),
    (r"MediaTek|聯發科", "公司", "發哥集團"), (r"Innolux|群創", "公司", "面板"), (r"Ibiden|Unimicron|欣興|ABF", "公司", "ABF"),
    (r"OpenAI|資料中心|GW|Stargate|Oracle", "公司", "資料中心/AIDC"), (r"Tesla|Optimus|機器人", "公司", "人形機器人"),
    (r"^AMD$|MI\d{3}|^Intel$|Qualcomm|Broadcom|Microsoft|Google|Meta|Amazon|Samsung|SK Hynix|Micron|生益科技|Shengyi|Foxconn|鴻海|Luxshare|立訊|Huawei|華為|Xiaomi|小米", "公司", ""),
    (r"ASIC|TPU|XPU|Trainium|MTIA", "產品", "ASIC"), (r"液冷|liquid cooling|CDU|散熱", "技術", "液冷"),
    (r"摺疊|折疊|foldable|Fold", "產品", "樞紐"), (r"衛星|Starlink|SpaceX", "產品", "低軌"),
    (r"^(GPU|AI|IC|US|GB|TB|KV|WIP|TB/s|B/in|L1)$", "忽略", ""),
]
GENERIC_EN = re.compile(r"^[A-Z][a-z]+$")


def keep(old: pd.Series, new: pd.Series) -> pd.Series:
    return old.where(old != "", new)


def main() -> int:
    # 1. 題材關鍵字
    kw = pd.read_csv(MAP / "題材關鍵字.csv", dtype=str, keep_default_na=False)
    col = "新聞關鍵字(|分隔)"
    for i, th in enumerate(kw["題材"]):
        if th in SYNONYMS and kw.at[i, col] in ("", th):
            kw.at[i, col] = "|".join(dict.fromkeys([th] + SYNONYMS[th].split("|")))
    kw.to_csv(MAP / "題材關鍵字.csv", index=False, encoding="utf-8-sig")

    # 2. 題材概念股
    t = pd.read_csv(MAP / "題材概念股.csv", dtype=str, keep_default_na=False)
    sys_ = (t["來源"] == "系統建議") & (t["股票代號"] != "")
    n = pd.to_numeric(t["系統統計_近一年共現次數"], errors="coerce").fillna(0)
    t.loc[sys_, "保留(Y/N)"] = keep(t.loc[sys_, "保留(Y/N)"], "Y")
    t.loc[sys_, "備註"] = keep(t.loc[sys_, "備註"], NOTE)
    t.loc[sys_, "關聯強度(1-3)"] = keep(t.loc[sys_, "關聯強度(1-3)"], (n[sys_] >= 10).map({True: "2", False: "1"}))
    dsl = t["來源"].isin(["DSL", "使用者"])
    t.loc[dsl, "關聯強度(1-3)"] = keep(t.loc[dsl, "關聯強度(1-3)"], "3")
    t.to_csv(MAP / "題材概念股.csv", index=False, encoding="utf-8-sig")
    members = t[(t["保留(Y/N)"] != "N") & (t["股票代號"] != "")].groupby("題材")["股票代號"].apply(list).to_dict()

    # 3. 對象概念股
    e = pd.read_csv(MAP / "對象概念股.csv", dtype=str, keep_default_na=False)
    sys_ = (e["來源"] == "系統建議") & (e["股票代號"] != "")
    n = pd.to_numeric(e["系統統計_近一年共現次數"], errors="coerce").fillna(0)
    e.loc[sys_, "保留(Y/N)"] = keep(e.loc[sys_, "保留(Y/N)"], "Y")
    e.loc[sys_, "關聯強度(1-3)"] = keep(e.loc[sys_, "關聯強度(1-3)"], (n[sys_] >= 10).map({True: "2", False: "1"}))
    e.loc[sys_, "角色"] = keep(e.loc[sys_, "角色"], NOTE)
    e.to_csv(MAP / "對象概念股.csv", index=False, encoding="utf-8-sig")

    # 4. 分析師關鍵字
    for path in MAP.glob("分析師關鍵字_*.csv"):
        k = pd.read_csv(path, dtype=str, keep_default_na=False)
        c_cls, c_stk, c_th = "我的分類(公司/產品/技術/材料/忽略)", "相關台股(代號|分隔)", "歸入題材"
        for i, term in enumerate(k["關鍵字"]):
            cls, th = "", ""
            for pat, a, b in TERM_RULES:
                if re.search(pat, term):
                    cls, th = a, b
                    break
            if not cls:
                if k.at[i, "台股代號"]:
                    cls = "公司"
                elif k.at[i, "系統判斷類型"] == "產品型號/料號":
                    cls = "產品"
                elif GENERIC_EN.match(term) and k.at[i, "文章數"] in ("1", "2"):
                    cls = "忽略"
            if not k.at[i, c_cls]:
                k.at[i, c_cls] = cls
            if not k.at[i, c_th]:
                k.at[i, c_th] = th
            if not k.at[i, c_stk]:
                k.at[i, c_stk] = k.at[i, "台股代號"] or "|".join(members.get(k.at[i, c_th], [])[:12])
        k.to_csv(path, index=False, encoding="utf-8-sig")
        print(path.name, len(k), k[c_cls].replace("", "(未分類)").value_counts().to_dict(),
              "有題材", (k[c_th] != "").sum())
    print("題材概念股", len(t), t["來源"].value_counts().to_dict(), "| 題材關鍵字", len(kw),
          "有同義詞", (kw[col].str.contains(r"\|")).sum(), "| 對象概念股", len(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
