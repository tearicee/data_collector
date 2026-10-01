#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
事件標籤規則 (重訊與新聞共用，純文字模式，不經 AI 模型)
================================================================
一則訊息可同時有多個標籤 (multi-label)。調整切入方向只要改這個檔：
  TAG_RULES   事件類別：text = 正則 (比對 主旨/標題，新聞另比對內文前段)；
              fields = 重訊說明的欄位名特徵 (命中任一即算，比主旨用詞穩定)。
  MODIFIERS   修飾標籤：首創/極端/轉折等「新鮮度用語」，與事件類別正交。
  ENTITIES    重量級對象：出現即加註 (用於「誰」參與了這件事)。
  THEMES      題材：用來算「個股/產業 × 題材」是否首次共現 (傳產切入半導體這類跨界)，
              以及收盤後找出題材概念股算市場反應。
  clarification_stance()  澄清公告的立場 (否認/證實/不評論)。
"""
import re

TAG_RULES = [
    # ---- 籌資 / 股本
    {"tag": "現金增資", "text": r"現金增資|(?<!表)現增|發行新股(?!.{0,6}(限制員工|員工認股))",
     "fields": ["本次增資資金用途", "公開銷售方式及股數"]},
    {"tag": "私募", "text": r"私募", "fields": ["私募有價證券種類", "私募對象及其與公司間關係"]},
    {"tag": "可轉債", "text": r"轉換公司債|可轉債|交換公司債|ECB|\bCB\b"},
    {"tag": "公司債", "text": r"普通公司債|(?<!轉換)(?<!交換)公司債"},
    {"tag": "融資/聯貸", "text": r"聯貸|聯合授信|授信合約|信用額度|信貸額度|循環信用|銀彈|籌資|募資"},
    {"tag": "策略合作", "text": r"攜手|聯手|結盟|合作(開發|案|協議|夥伴)?|合資|跨足|切入|進軍|打入|搶進|布局|大啖|卡位"},
    {"tag": "掛牌/承銷", "text": r"初次上[市櫃]|掛牌|過額配售|穩定價格|承銷價|競價拍賣|詢價圈購|上[市櫃]案"},
    {"tag": "海外存託憑證", "text": r"存託憑證|GDR|ADR(?!.{0,4}(收|漲|跌))"},
    {"tag": "減資", "text": r"減資", "fields": ["減資比率", "減資基準日", "主管機關核准減資日期"]},
    {"tag": "庫藏股", "text": r"庫藏股|買回(本公司)?(股份|股票)", "fields": ["原預定買回之數量(股)", "本次買回股份數量(股)"]},
    {"tag": "面額變更/分割", "text": r"面額|股票分割|拆股|\d\s*拆\s*\d+"},
    # ---- 財務數字
    {"tag": "自結", "text": r"自結|達公布注意交易資訊|注意交易資訊標準|月份?(之)?(損益|營運成績|獲利)", "fields": ["財務業務資訊"]},
    {"tag": "財報", "text": r"財務報告|財報|季報|年報|每股(盈餘|純益|獲利)|EPS|三率"},
    {"tag": "營收", "text": r"營收|營業收入"},
    {"tag": "財測", "text": r"財測|財務預測|展望|上修|下修|指引"},
    {"tag": "股利", "text": r"股利|配息|配股|(現金|股息|股利)殖利率|盈餘分[派配]|除權|除息", "fields": ["發放股利種類及金額"]},
    # ---- 股權 / 經營權
    {"tag": "併購", "text": r"併購|合併案|吸收合併|簡易合併|合併基準日|合併契約|與.{1,14}合併(?!財|報|營收)|公開收購|收購|"
                           r"股份轉換|換股比|入股|策略(結盟|聯盟|投資|合作)"},
    {"tag": "經營權", "text": r"經營權|改選|董事長.{0,6}(異動|辭|請辭|解任)|總經理.{0,6}(異動|辭)",
     "fields": ["人員別（請輸入董事長或總經理）"]},
    {"tag": "大股東轉讓", "text": r"申報轉讓|出脫|出清|釋股|鉅額(交易|轉讓)"},
    {"tag": "資產交易", "text": r"(取得|處分|出售|買下|購置|賣).{0,14}(不動產|土地|廠房|廠辦|設備|股權|持股|使用權資產)|委建|承攬契約"},
    {"tag": "金融投資", "text": r"(取得|處分|買賣).{0,10}(有價證券|理財|基金|債券|受益憑證|結構式)|授信資產"},
    {"tag": "背書保證/資金貸與", "text": r"背書保證|資金貸與"},
    {"tag": "人事異動", "text": r"(董事|獨立董事|監察人|經理人|發言人|主管|委員|資安長|會計師).{0,12}(異動|辭任|辭職|解任|變動|改派|委任|更換)|競業禁止"},
    {"tag": "法說會", "text": r"法人說明會|法說會|受邀參加|投資論壇|業績發表會|前瞻論壇"},
    {"tag": "股東會", "text": r"股東(常|臨時)?會"},
    {"tag": "資本支出/擴廠", "text": r"資本支出|擴廠|擴產|建廠|新廠|產能.{0,4}(擴|倍增|開出)"},
    # ---- 營運事件
    {"tag": "訂單/合約", "text": r"訂單|大單|得標|標案|簽約|簽署.{0,8}(合約|契約|協議|備忘錄)|打入.{0,8}(供應鏈|鏈)|認證"},
    {"tag": "報價/供需", "text": r"漲價|調漲|跌價|降價|報價|缺貨|供不應求|供過於求|產能滿載|庫存"},
    {"tag": "新產品/技術", "text": r"首款|首個|發表|發布|推出|量產|新品|新藥|技術突破"},
    {"tag": "新藥/藥證", "text": r"臨床|解盲|藥證|FDA|EMA|查驗登記|孤兒藥", "fields": ["研發新藥名稱或代號"]},
    {"tag": "澄清", "text": r"澄清|媒體報導|說明.{0,6}報導", "fields": ["傳播媒體名稱", "報導內容"]},
    # 監管處分：交易方式被限制 (全額交割/變更交易方法/處置/停止買賣/終止上市)，對股價影響直接
    {"tag": "監管處分", "text": r"變更交易方法|全額交割|處置(股|有價證券|期間)|列為處置|分盤交易|(?<!憑證同時)(?<!憑證)停止買賣|終止上[市櫃]買賣|下市|打入全額|恢復(普通|一般)交易"},
    {"tag": "籌資進度", "text": r"代收(及存儲)?(價|股|債)款|存儲專戶|收足(股|債)款|認股基準日|繳款"},
    {"tag": "訴訟/裁罰", "text": r"訴訟|起訴|判決|裁罰|罰鍰|檢調|搜索|調查|內線交易|掏空|違約",
     "fields": ["法律事件之當事人", "裁罰金額(元)"]},
    {"tag": "災害/資安", "text": r"火災|大火|地震|颱風|爆炸|停電|停工|停產|罷工|駭客|資安|網路安全事件",
     "fields": ["可能獲得保險理賠之金額"]},
    # ---- 外部環境 (新聞為主)
    {"tag": "政策-美國", "scope": "news", "text": r"川普|白宮|美國.{0,8}(關稅|禁令|制裁|法案|政策)|232條款|晶片法|出口管制|實體清單|聯準會|Fed"},
    {"tag": "政策-中國", "scope": "news", "text": r"國台辦|陸委會|中共|北京|中國.{0,8}(禁|政策|制裁|補貼|反傾銷|查稅)|ECFA|陸客"},
    {"tag": "政策-台灣", "scope": "news", "text": r"行政院|立法院|立院|金管會|中央銀行|央行(總裁|理監事|升息|降息|打房|鬆綁)|經濟部|國發會|財政部|證交所|櫃買中心|打房|選擇性信用管制"},
    {"tag": "關稅/貿易", "scope": "news", "text": r"關稅|反傾銷|貿易戰|出口管制|禁運|制裁"},
    {"tag": "戰爭/地緣", "scope": "news", "text": r"開戰|宣戰|戰爭|空襲|飛彈|軍演|封鎖|紅海|停火"},
    {"tag": "指數調整", "scope": "news", "text": r"MSCI|富時|FTSE|台灣50|臺灣50|0050|0056|成分股|納入|剔除|季度調整"},
    {"tag": "機構評等", "scope": "news", "text": r"目標價|評等|升評|降評|調升|調降|外資.{0,6}(喊|看|報告)|大摩|高盛|小摩|瑞銀|美銀"},
    {"tag": "供應鏈連動", "scope": "news", "text": r"供應鏈|概念股|受惠|衝擊|帶動|點名|沾光|遭殃"},
]

MODIFIERS = [
    {"tag": "首創", "text": r"首次|首度|首家|首例|首見|首創|首款|首個|第一(家|個|槍|例)|第一次(?!.{0,3}(有|無)?擔保|.{0,8}(公司債|私募|股東|董事會|現金增資))|史上首|全球首|創舉|前所未見"},
    {"tag": "極端", "text": r"創.{0,8}(新高|新低|紀錄|最高|最低|之最)|歷史(新高|新低|次高)|史上最|罕見|暴增|暴漲|暴跌|腰斬|翻倍|倍增|"
                            r"\d+\s*年來?(新高|新低|首見|最大|最高|最低|罕見)|\d+\s*個月來?(新高|新低)"},
    {"tag": "轉折", "text": r"解禁|鬆綁|開放|解除|取消|轉盈|轉虧|由虧轉盈|逆轉|急轉|喊卡|終止|撤回|重啟|恢復|翻盤"},
    {"tag": "意外", "text": r"意外|突|驚|爆|不如預期|優於預期|超(出|乎)預期|遜於預期|落空"},
    {"tag": "傳聞", "text": r"傳出?|據傳|市場傳|消息人士|擬|有望|可望|恐|或將"},
    # 供應鏈變數：與市場既有預期落差大的訊號 (分析師/產業調查用語)，預期帶來股價振幅
    {"tag": "供應鏈變數", "text": r"取代|變數|重啟|轉單|改用|改採|放棄|延後|砍單|降低出貨|縮減|停產|低於預期|不如預期|超出預期|超預期|"
                                 r"首度導入|新增供應商|第二供應商|領先供應商|測試中|重新設計|改變|逆轉|打破"},
    # 雜訊：已成定局的總經數據、公司活動/公關
    {"tag": "總經數據", "text": r"PCE|CPI|非農|就業數據|失業率|PMI|GDP|FOMC|利率決議|美債殖利率|公債殖利率|外匯存底|進出口"},
    {"tag": "公司活動", "text": r"分紅|家庭日|尾牙|員工旅遊|員工餐廳|校園徵才|徵才|公益|捐贈|頒獎|獲獎|得獎|評比|榮獲|永續獎|論壇登場|記者會|開幕"},
]

ENTITIES = {
    "美系巨頭": r"輝達|NVIDIA|Nvidia|蘋果|Apple|微軟|Microsoft|亞馬遜|Amazon|AWS|Google|谷歌|Alphabet|Meta|特斯拉|Tesla|"
                r"OpenAI|超微|AMD|英特爾|Intel|博通|Broadcom|高通|Qualcomm|美光|Micron|SpaceX|甲骨文|Oracle|Marvell|戴爾|Dell|美超微",
    "台系龍頭": r"台積電|鴻海|聯發科|廣達|台達電|日月光|聯電|大立光|中華電|富邦金|國泰金",
    "日韓陸大廠": r"三星|SK海力士|SK Hynix|索尼|Sony|軟銀|SoftBank|豐田|華為|比亞迪|寧德時代|中芯|小米|阿里|騰訊|立訊",
    "政要": r"川普|拜登|習近平|鮑爾|黃仁勳|馬斯克|郭台銘|魏哲家|巴菲特|波克夏",
    "主權/機構": r"國發基金|勞動基金|主權基金|波克夏|貝萊德|淡馬錫|阿布達比",
}

THEMES = {
    "半導體": r"半導體|晶圓|晶片|IC設計|封測|台積電供應鏈",
    "先進封裝": r"先進封裝|CoWoS|CoPoS|FOPLP|面板級封裝|扇出|SoIC|3D封裝|異質整合",
    "玻璃基板": r"玻璃基板|玻璃載板|TGV|玻璃芯",
    "矽光子/CPO": r"矽光子|CPO|共同封裝光學|光通訊|光收發|光模組",
    "AI伺服器": r"AI伺服器|AI 伺服器|GPU|ASIC|算力|資料中心|AIDC|液冷|機櫃",
    "記憶體": r"記憶體|DRAM|NAND|HBM|快閃",
    "散熱": r"散熱|均熱片|水冷|液冷",
    "PCB/載板": r"PCB|載板|銅箔基板|CCL|ABF",
    "機器人": r"機器人|人形機器|Optimus|機械手臂|減速機",
    "低軌衛星/太空": r"低軌衛星|衛星|太空|Starlink|星鏈|火箭",
    "太陽能": r"太陽能|光電(板|場|案)|矽晶圓太陽|光伏",
    "儲能/重電": r"儲能|重電|變壓器|電網|強韌電網|電力設備",
    "風電": r"風電|離岸風",
    "核能": r"核能|核電|核融合|SMR",
    "電動車": r"電動車|EV|充電樁|自駕|Robotaxi|Cybercab",
    "無人機/軍工": r"無人機|軍工|國防|軍備|飛彈|潛艦",
    "量子": r"量子",
    "生技新藥": r"新藥|生技|CDMO|減重藥|GLP-1|細胞治療",
    "航運": r"航運|貨櫃|運價|散裝|SCFI|BDI",
    "鋼鐵/原物料": r"鋼價|不銹鋼|鎳價|銅價|鋁價|盤價|原物料",
    "面板": r"面板|Micro LED|OLED|顯示器",
    "營建/資產": r"營建|房市|都更|建案|打房",
    "金融": r"金控|壽險|銀行股|金融股|升息|降息",
    "觀光/內需": r"觀光|旅遊|餐飲|陸客|百貨",
    "加密貨幣": r"比特幣|加密貨幣|以太幣|穩定幣",
}


# ---- 使用者維護的對照表 (D:/mops/news/mapping/)：有檔案就併入，沒有就只用上面的內建值
MAP_DIR = "/mnt/d/mops/news/mapping"
ENTITY_STOCKS: dict = {}     # 對象 → [台股代號]   (對象概念股.csv，保留≠N)
THEME_STOCKS: dict = {}      # 題材 → [台股代號]   (題材概念股.csv，保留≠N)


def _kw_regex(words) -> str:
    """關鍵字清單 → 正則。純英數的詞加英文字邊界 (避免 RE 命中 REIT)；過短或純數字的略過。"""
    parts = []
    for w in dict.fromkeys(x.strip() for x in words if x and x.strip()):
        if len(w) < 2 or w.isdigit():
            continue
        if re.fullmatch(r"[A-Za-z0-9 .\-/+]+", w):
            if len(w) < 3:
                continue
            parts.append(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])")
        else:
            parts.append(re.escape(w))
    return "|".join(parts)


def _load_mapping() -> None:
    import csv
    import os

    def rows(name):
        path = os.path.join(MAP_DIR, name)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))

    for r in rows("題材關鍵字.csv"):
        rx = _kw_regex((r.get("新聞關鍵字(|分隔)") or "").split("|"))
        if rx:
            THEMES[r["題材"]] = rx          # 同名題材以對照表為準
    for r in rows("題材概念股.csv"):
        if r.get("股票代號") and r.get("保留(Y/N)", "").upper() != "N":
            THEME_STOCKS.setdefault(r["題材"], []).append(r["股票代號"])
    alias = {}
    for r in rows("對象概念股.csv"):
        ent = r.get("對象", "")
        if not ent:
            continue
        alias.setdefault(ent, set()).update([ent] + (r.get("別名(|分隔)") or "").split("|"))
        if r.get("股票代號") and r.get("保留(Y/N)", "").upper() != "N":
            ENTITY_STOCKS.setdefault(ent, []).append(r["股票代號"])
    ENTITY_ALIAS.update({e: re.compile(rx) for e, a in alias.items() if (rx := _kw_regex(a))})


ENTITY_ALIAS: dict = {}      # 對象 → 別名正則
_load_mapping()
_THEME_RE = [(k, re.compile(v)) for k, v in THEMES.items()]


def mapped_entities(title: str) -> list:
    """標題提到的「對象」(對象概念股.csv)。"""
    return [e for e, rx in ENTITY_ALIAS.items() if rx.search(title or "")]

DENY_RE = re.compile(r"並無|並未|尚無|尚未|未有|沒有|非屬|不實|純屬|臆測|無此|已(於.{0,10})?(結束|停止|終止|退出)|與本公司無關|不予評論|無法評論")
CONFIRM_RE = re.compile(r"屬實|確有|確實|已簽|已取得|已接獲|正在(洽談|評估|進行)")


def clarification_stance(answer: str) -> str:
    """澄清公告「因應措施」的立場：否認 / 制式否認 / 證實 / 未表態。"""
    if DENY_RE.search(answer or ""):
        # 「未提供財務預測，係屬媒體臆測」是媒體寫營收/獲利展望時的制式回覆，資訊量低
        return "制式否認" if re.search(r"財務預測|財測|獲利預(估|測)|營收預(估|測)|預測性", answer) else "否認"
    if CONFIRM_RE.search(answer or ""):
        return "證實"
    return "未表態"


def themes_of(title: str, body: str = "") -> dict:
    """{"title": [標題命中的題材], "all": [標題+內文前 600 字命中的題材]}"""
    head = f"{title}\n{(body or '')[:600]}"
    return {"title": [k for k, rx in _THEME_RE if rx.search(title or "")],
            "all": [k for k, rx in _THEME_RE if rx.search(head)]}


_TAG_RE = [(r["tag"], re.compile(r["text"]), r.get("fields", [])) for r in TAG_RULES]
_NEWS_ONLY = {r["tag"] for r in TAG_RULES if r.get("scope") == "news"}
_MOD_RE = [(r["tag"], re.compile(r["text"])) for r in MODIFIERS]
TITLE_ONLY_MODS = {"總經數據", "公司活動"}   # 雜訊類只看標題，內文順帶提到不算
_ENT_RE = [(k, re.compile(v)) for k, v in ENTITIES.items()]


def tag_text(title: str, body: str = "", field_names=(), source: str = "news") -> dict:
    """回傳 {"tags": [...], "modifiers": [...], "entities": {類別: [名稱...]}}。
    source="mops"：只認主旨與欄位名特徵 (說明是制式表單，內文比對會誤判)，且不套用 scope=news 的標籤。
    source="news"：標題命中即算；BODY_OK 標籤另看內文前 400 字。"""
    mops = source == "mops"
    head = title if mops else f"{title}\n{(body or '')[:400]}"
    names = set(field_names)
    tags = [t for t, rx, fs in _TAG_RE
            if not (mops and t in _NEWS_ONLY) and (rx.search(title) or names.intersection(fs))]
    if not mops:
        tags += [t for t, rx, fs in _TAG_RE if t not in tags and rx.search(head) and t in BODY_OK]
    mods = [t for t, rx in _MOD_RE if rx.search(title if t in TITLE_ONLY_MODS else head)]
    ents = {}
    for k, rx in _ENT_RE:
        found = sorted({m.group(0) for m in rx.finditer(f"{title}\n{body or ''}")})
        if found:
            ents[k] = found
    title_tags = [t for t, rx, fs in _TAG_RE if t in tags and (rx.search(title) or names.intersection(fs))]
    return {"tags": tags, "title_tags": title_tags, "modifiers": mods, "entities": ents}


# 內文前段命中也採計的標籤 (用詞明確、不易誤判者)；其餘只認標題
BODY_OK = {"現金增資", "私募", "可轉債", "減資", "庫藏股", "併購", "訂單/合約", "報價/供需",
           "新藥/藥證", "訴訟/裁罰", "災害/資安", "關稅/貿易", "指數調整", "大股東轉讓"}
