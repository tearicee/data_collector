#!/usr/bin/env python3
"""各投信申購買回清單 (PCF) 的擷取: 每日申購贖回 + 每日成分。

與 raw_holdings/issuers/ 的 adapter 打的是同一批網站，這裡重用它們的連線、代號對照與
瀏覽器啟動 (base.REGISTRY)，另外做兩件 adapter 沒做的事: 指定日期查詢、取出申購贖回數字。
輸出欄位與寫法對齊 2026-10-02 那份歷史資料 (各投信的代碼格式、哪些欄有值都照舊)。

每個來源提供:
    summary(code, day)  -> Summary | None     申購贖回 (一檔一日一列)
    holdings(code, day) -> Holdings | None    成分
day 是要查的「交易日」(YYYY-MM-DD，歷史資料的鍵；各投信意義不同，見 store.py)。回傳物件的
trade_day 以來源回應自己標的日期為準 —— 富邦對沒有資料的日子會回最近一筆，不能相信查詢日。
asof_day 是來源明示的資料日；來源沒給就留 None，由 collect.py 依規則補 (只補國內型)。

各家重點:
    元大   GET  etfapi…/bridge?FuncId=PCF/Daily&ticker=&date=YYYYMMDD     anndate=公告日 trandate=資料日
    國泰   GET  cwapi…/BuySale/GetBuySale (申購贖回)  ETF/GetETFDetail*List (成分)   查無回 returnCode 4005
    富邦   GET  Pcf.aspx?ddate= (申購贖回)  Assets.aspx?ddate= (成分，頁面有「資料日期：」)
    復華   GET  /api/ETFPcf?pcfDate= (申購贖回)  /api/assets?qDate= (成分)
    統一   POST /ETF/Transaction/GetPCF {fundCode, date: 民國 yyy/mm/dd, specificDate: true}
    群益   POST /CFWeb/api/etf/buyback {fundId, date}   (playwright)   date1=公告日 date2=資料日
    中信   清單頁填日期按搜尋，攔 /API/etf/Buyback 回應 (playwright)   公告日 / 淨值日期
"""
from __future__ import annotations
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "raw_holdings"))
import base  # noqa: E402
import issuers  # noqa: E402,F401  觸發 adapter 註冊
from issuers import capital, cathay, ctbc, fubon, fuhhwa, unipsit, yuanta  # noqa: E402

TIMEOUT = (10, 30)
TW = timezone(timedelta(hours=8))

SRC_HOLDINGS = "持股明細"
SRC_PCF = "PCF公告持股"
SRC_BASKET = "申購買回清單(每申購基數)"


@dataclass
class Summary:
    trade_day: str
    announce_day: str | None
    asof_day: str | None
    net_units: float | None       # 申購贖回淨增減單位數
    units: float | None           # 已發行單位數
    nav: float | None             # 每單位淨值
    net_assets: float | None      # 基金淨資產
    basket_units: float | None    # 實物申贖基數
    url: str


@dataclass
class Holdings:
    trade_day: str
    asof_day: str | None
    source: str
    rows: list[dict] = field(default_factory=list)   # 類別, 成分代碼, 成分名稱, 數量, 金額, 權重(%), 到期月份


def num(v) -> float | None:
    """'NT$1,234.5' / '3.966%' / 12 → float；空值與非數字回 None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return None if v != v else float(v)
    s = re.sub(r"[,%\s]|NT\$|TWD|USD", "", str(v))
    if s in ("", "-", "N/A", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def iso(v) -> str | None:
    """'20260930' / '2026/09/30' / '2026-9-30…' → '2026-09-30'。"""
    m = re.search(r"(20\d\d)\D?(\d{1,2})\D?(\d{1,2})", str(v or ""))
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None


def net_date(v) -> str | None:
    """.NET 序列化的日期 → 'YYYY-MM-DD'；無效值 (0001 年) 回 None。
    統一的伺服器兩種寫法交替出現: '/Date(1790611200000)/' (毫秒，換成台灣時區) 與 '2026-09-29T00:00:00'。"""
    m = re.search(r"Date\((-?\d+)\)", str(v or ""))
    if m:
        if int(m.group(1)) <= 0:
            return None
        return datetime.fromtimestamp(int(m.group(1)) / 1000, TW).strftime("%Y-%m-%d")
    return iso(v)


def row(kind, code, name, qty, amount=None, weight=None, expiry=None) -> dict | None:
    code = str(code or "").strip()
    if not code:
        return None
    expiry = str(expiry or "").strip() or None
    return {"類別": kind, "成分代碼": code, "成分名稱": (str(name).strip() if name is not None else None),
            "數量": num(qty), "金額": num(amount), "權重(%)": num(weight), "到期月份": expiry}


class Source:
    issuer = ""          # 資料集裡的投信名
    adapter_key = ""     # raw_holdings adapter 的註冊名
    summary_forward = True     # 申購贖回的「交易日」是公告日 → 收盤後就查得到下一個交易日
    holdings_forward = True

    def __init__(self):
        self.ad = base.REGISTRY[self.adapter_key]
        self._cache: dict = {}

    def summary(self, code: str, day: str) -> Summary | None:
        raise NotImplementedError

    def holdings(self, code: str, day: str) -> Holdings | None:
        raise NotImplementedError

    def close(self):
        self._cache.clear()
        try:
            self.ad.close()
        except Exception:                                # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
class Yuanta(Source):
    issuer, adapter_key = "元大", "元大"

    def _url(self, code: str, day: str) -> str:
        return yuanta.BRIDGE_URL + "?" + urlencode({
            "APIType": "ETFAPI", "CompanyName": "YUANTAFUNDS", "PageName": f"/tradeInfo/pcf/{code}",
            "DeviceId": "null", "FuncId": "PCF/Daily", "AppName": "ETF", "Device": "3", "Platform": "ETF",
            "ticker": code, "date": day.replace("-", "")})

    def _pcf(self, code: str, day: str) -> dict:
        key = (code, day)
        if key not in self._cache:
            h = self.ad.headers()
            h["Referer"] = yuanta.REFERER
            r = self.ad.session.get(self._url(code, day), headers=h, timeout=TIMEOUT)
            r.raise_for_status()
            d = r.json()
            if isinstance(d, dict) and "Data" in d and "PCF" not in d:
                d = d.get("Data") or {}
            self._cache[key] = d if isinstance(d, dict) else {}
        return self._cache[key]

    def summary(self, code, day):
        pcf = self._pcf(code, day).get("PCF") or {}
        ann = iso(pcf.get("anndate"))
        if not ann:
            return None
        return Summary(ann, ann, iso(pcf.get("trandate")), num(pcf.get("issuesdiff")), num(pcf.get("osunit")),
                       num(pcf.get("nav")), num(pcf.get("totalav")), num(pcf.get("baseunit")), self._url(code, ann))

    def holdings(self, code, day):
        d = self._pcf(code, day)
        pcf = d.get("PCF") or {}
        ann = iso(pcf.get("anndate"))
        if not ann:
            return None
        fw = d.get("FundWeights") or {}
        rows = []
        for key, kind in (("StockWeights", "股票"), ("BondWeights", "債券"), ("ETFWeights", "ETF"),
                          ("FutureWeights", "期貨")):
            for x in fw.get(key) or []:
                qty = x.get("FACE_AMT") if kind == "債券" else x.get("qty")     # 債券記面額
                rows.append(row(kind, x.get("code"), x.get("name"), qty, None, x.get("weights"), x.get("ym")))
        source = SRC_HOLDINGS
        if not any(rows):                                # 沒有持股明細 → 退回每申購基數的實物清單
            source = SRC_BASKET
            rows = [row("股票", x.get("stkcd"), x.get("name"), x.get("qty"))
                    for x in (d.get("InKind") or {}).get("FundComposition") or []]
        rows = [x for x in rows if x]
        return Holdings(ann, iso(pcf.get("trandate")), source, rows) if rows else None


# --------------------------------------------------------------------------- #
class Cathay(Source):
    issuer, adapter_key = "國泰", "國泰"
    holdings_forward = False           # 持股明細以持股日為鍵，沒有「明天」的資料
    BUYSALE = "https://cwapi.cathaysite.com.tw/api/BuySale/GetBuySale"
    LISTS = (("GetETFDetailStockList", "股票", "stockCode", "stockName", "volumn", None, "weights", None),
             ("GetETFDetailBondList", "債券", "bondNo", "bondName", "parValue", "mkval", "ntMkval", None),
             ("GetETFDetailFutureList", "期貨", "ftNo", "ftName", "volumn", None, "ntMkval", "ftDate"))

    def _get(self, url: str, params: dict) -> dict:
        h = self.ad.headers()
        h["Referer"] = cathay.REFERER
        r = self.ad.session.get(url, params=params, headers=h, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        return d if str(d.get("returnCode")) == "2000" else {}

    def _fund(self, code: str) -> str:
        fc = self.ad._load_map().get(code)
        if not fc:
            raise ValueError(f"國泰清單無此代號 {code}")
        return fc

    def summary(self, code, day):
        fc = self._fund(code)
        slash = day.replace("-", "/")
        res = self._get(self.BUYSALE, {"FundCode": fc, "SearchDate": slash, "IsTest": "false"}).get("result") or {}
        ann = iso(res.get("date"))
        if not ann:
            return None
        url = f"{self.BUYSALE}?FundCode={fc}&SearchDate={quote(ann.replace('-', '/'), safe='')}&IsTest=false"
        return Summary(ann, ann, iso(res.get("preDateC")), num(res.get("diffUnit")), num(res.get("totUnit")),
                       num(res.get("nav")), num(res.get("aum")), num(res.get("basketUnit")), url)

    def holdings(self, code, day):
        fc = self._fund(code)
        params = {"FundCode": fc, "SearchDate": day.replace("-", "/")}
        rows = []
        for ep, kind, c_id, c_name, c_qty, c_amt, c_w, c_exp in self.LISTS:
            res = self._get(f"{cathay.API}/{ep}", params).get("result")
            for x in res if isinstance(res, list) else []:
                rows.append(row(kind, x.get(c_id), x.get(c_name), x.get(c_qty),
                                x.get(c_amt) if c_amt else None, x.get(c_w), x.get(c_exp) if c_exp else None))
        rows = [x for x in rows if x]
        return Holdings(day, day, SRC_HOLDINGS, rows) if rows else None      # 查無會回 4005，有資料即為該日


# --------------------------------------------------------------------------- #
class Fubon(Source):
    issuer, adapter_key = "富邦", "富邦"
    summary_forward = holdings_forward = False      # 兩種都以資料日為鍵
    PCF_URL = "https://websys.fsit.com.tw/FubonETF/Trade/Pcf.aspx"
    DATA_DATE = re.compile(r"資料日期[:：]\s*(20\d\d/\d\d/\d\d)")
    ANNOUNCE = re.compile(r"申購買回清單\s*(20\d\d/\d\d/\d\d)")

    def _html(self, url: str, params: dict) -> str:
        h = self.ad.headers()
        h["Referer"] = fubon.REFERER
        r = self.ad.session.get(url, params=params, headers=h, timeout=TIMEOUT)
        r.raise_for_status()
        r.encoding = "utf-8"
        return r.text

    def _assets(self, code: str, day: str) -> tuple[str | None, list[dict]]:
        """資產頁 → (頁面標示的資料日期, 成分列)。查詢日沒有資料時網站回最近一筆，資料日期才是真的。"""
        key = ("assets", code, day)
        if key in self._cache:
            return self._cache[key]
        import lxml.html
        html = self._html(fubon.ASSETS_URL, {"stkId": code, "ddate": day.replace("-", "/"), "lan": "TW"})
        doc = lxml.html.fromstring(html)
        m = self.DATA_DATE.search(re.sub(r"\s+", " ", doc.text_content()))
        rows = []
        for table in doc.xpath("//table"):
            trs = table.xpath(".//tr")
            if len(trs) < 2:
                continue
            head = [c.text_content().strip() for c in trs[0].xpath("./td|./th")]
            if not head or ("代碼" not in head[0] and "代號" not in head[0]):
                continue
            kind = head[0].replace("代碼", "").replace("代號", "").strip()
            if not kind:                                # 表頭只有「代碼」的是附買回等現金管理部位，不算成分
                continue
            ai = next((i for i, x in enumerate(head) if "金額" in x), None)
            wi = next((i for i, x in enumerate(head) if "權重" in x or "比例" in x), len(head) - 1)
            for tr in trs[1:]:
                td = [c.text_content().strip() for c in tr.xpath("./td")]
                if len(td) <= wi or not td[0] or any(k in td[0] for k in ("合計", "小計", "總計", "合  計")):
                    continue
                rows.append(row(kind, td[0], td[1] if len(td) > 1 else None, td[2] if len(td) > 2 else None,
                                td[ai] if ai is not None and ai < len(td) else None, td[wi]))
        self._cache[key] = (iso(m.group(1)) if m else None, [x for x in rows if x])
        return self._cache[key]

    def holdings(self, code, day):
        data_day, rows = self._assets(code, day)
        return Holdings(data_day, data_day, SRC_HOLDINGS, rows) if data_day and rows else None

    def summary(self, code, day):
        import lxml.html
        data_day, _ = self._assets(code, day)           # 清單頁沒有資料日，借資產頁的來定位
        if not data_day:
            return None
        doc = lxml.html.fromstring(self._html(self.PCF_URL, {"stkId": code, "ddate": data_day.replace("-", "")}))
        pairs = {}
        for li in doc.xpath("//li[count(p)=2]"):
            k, v = (p.text_content().strip() for p in li.xpath("./p"))
            pairs.setdefault(k, v)
        if num(pairs.get("每受益權單位淨資產價值")) is None:
            return None
        m = self.ANNOUNCE.search(re.sub(r"\s+", " ", doc.text_content()))
        basket = next((v for k, v in pairs.items() if "基數之受益權單位數" in k), None)
        return Summary(data_day, iso(m.group(1)) if m else None, None, num(pairs.get("與前日已發行單位差異數")),
                       num(pairs.get("已發行受益權單位總數")), num(pairs.get("每受益權單位淨資產價值")),
                       num(pairs.get("基金淨資產價值")), num(basket),
                       f"{self.PCF_URL}?stkId={code}&ddate={data_day.replace('-', '')}")


# --------------------------------------------------------------------------- #
class FuhHwa(Source):
    issuer, adapter_key = "復華", "復華"
    holdings_forward = False

    def _fund(self, code: str) -> str:
        self.ad._ensure()
        fid = self.ad._map.get(code)
        if not fid:
            raise ValueError(f"復華清單無此代號 {code}")
        return fid

    def _first(self, path: str, params: dict) -> dict:
        r = self.ad.session.get(f"{fuhhwa.BASE}{path}", params=params, headers=fuhhwa.UA, timeout=TIMEOUT)
        r.raise_for_status()
        res = r.json().get("result") or []
        return res[0] if res and isinstance(res[0], dict) else {}

    def summary(self, code, day):
        fid = self._fund(code)
        res = self._first("/api/ETFPcf", {"fundID": fid, "pcfDate": day.replace("-", "")})
        ann = iso(res.get("postDate"))
        if not ann or num(res.get("pnav")) is None:
            return None
        return Summary(ann, ann, None, num(res.get("qDiff")), num(res.get("qIssue")), num(res.get("pnav")),
                       num(res.get("nav")), num(res.get("sharesPerUnit")),
                       f"{fuhhwa.BASE}/api/ETFPcf?fundID={fid}&pcfDate={ann.replace('-', '')}")

    def holdings(self, code, day):
        res = self._first("/api/assets", {"fundID": self._fund(code), "qDate": day.replace("-", "/")})
        data_day = iso(res.get("dDate"))
        rows = [row(str(x.get("ftype") or "").strip(), x.get("stockid"), x.get("stockname"), x.get("qshare"),
                    x.get("mvalue"), x.get("prate_addaccint"))
                for x in res.get("detail") or [] if str(x.get("ftype") or "").strip() not in fuhhwa.SKIP_TYPES]
        rows = [x for x in rows if x]
        return Holdings(data_day, data_day, SRC_HOLDINGS, rows) if data_day and rows else None


# --------------------------------------------------------------------------- #
class President(Source):
    issuer, adapter_key = "統一", "統一"
    URL = f"{unipsit.BASE}/ETF/Transaction/GetPCF"

    def _pcf(self, code: str, day: str) -> dict:
        key = (code, day)
        if key not in self._cache:
            self.ad._ensure()
            fc = self.ad._map.get(code)
            if not fc:
                raise ValueError(f"統一清單無此代號 {code}")
            y, m, d = day.split("-")
            body = {"fundCode": fc, "date": f"{int(y) - 1911}/{m}/{d}", "specificDate": True}
            h = dict(unipsit.UA, **{"X-Requested-With": "XMLHttpRequest", "Origin": unipsit.BASE,
                                    "Content-Type": "application/json; charset=utf-8",
                                    "Referer": f"{unipsit.BASE}/ETF/Transaction/PCF?fundCode={fc}"})
            r = self.ad.session.post(self.URL, data=json.dumps(body), headers=h, timeout=TIMEOUT)
            r.raise_for_status()
            self._cache[key] = r.json() if "json" in r.headers.get("content-type", "") else {}
        return self._cache[key]

    def summary(self, code, day):
        items = {p.get("PCFCode"): p for p in self._pcf(code, day).get("pcf") or []}
        head = items.get("P_UNIT") or {}
        ann = net_date(head.get("PostDate"))
        if not ann:
            return None
        amt = lambda k: num((items.get(k) or {}).get("Amount"))          # noqa: E731
        basket = next((num(p.get("Amount")) for k, p in items.items() if k and k.endswith("BASEUNIT")), None)
        return Summary(ann, ann, net_date(head.get("TranDate")), amt("DIFF_UNIT"), amt("OUT_UNIT"), amt("P_UNIT"),
                       amt("NAV"), basket, self.URL)

    def holdings(self, code, day):
        d = self._pcf(code, day)
        head = next((p for p in d.get("pcf") or [] if p.get("PCFCode") == "P_UNIT"), {})
        ann = net_date(head.get("PostDate"))
        if not ann:
            return None
        rows, asof = [], None
        for a in d.get("asset") or []:
            name = str(a.get("AssetName") or "")
            kind = next((k for k in ("期貨", "股票", "債券", "選擇權", "基金") if name.startswith(k)), name)
            for x in a.get("Details") or []:
                asof = asof or net_date(x.get("TranDate"))
                rows.append(row(kind, x.get("DetailCode"), x.get("DetailName"), x.get("Share"), x.get("Amount"),
                                x.get("NavRate"), x.get("MTH")))
        rows = [x for x in rows if x]
        return Holdings(ann, asof or net_date(head.get("TranDate")), SRC_HOLDINGS, rows) if rows else None


# --------------------------------------------------------------------------- #
class Capital(Source):
    issuer, adapter_key = "群益", "群益"
    URL = f"{capital.BASE}/etf/buyback"

    def _data(self, code: str, day: str) -> dict:
        key = (code, day)
        if key not in self._cache:
            self.ad._ensure()
            fund_id = self.ad._map.get(code)
            if not fund_id:
                raise ValueError(f"群益清單無此代號 {code}")
            r = self.ad._page.request.post(self.URL, data=json.dumps({"fundId": fund_id, "date": day}),
                                           headers={"Content-Type": "application/json",
                                                    "Referer": capital.BUYBACK_PAGE})
            d = capital._decode_json(r) or {}
            self._cache[key] = d.get("data") or {}
        return self._cache[key]

    def summary(self, code, day):
        pcf = self._data(code, day).get("pcf") or {}
        ann = iso(pcf.get("date1"))
        if not ann:
            return None
        return Summary(ann, ann, iso(pcf.get("date2")), num(pcf.get("disUnit")), num(pcf.get("totUnit")),
                       num(pcf.get("pUnit")), num(pcf.get("nav")), num(pcf.get("tUnit")), self.URL)

    def holdings(self, code, day):
        data = self._data(code, day)
        pcf = data.get("pcf") or {}
        ann = iso(pcf.get("date1"))
        if not ann:
            return None
        rows = [row("股票", s.get("stocNo"), s.get("stocName"), s.get("share"), None, s.get("weight"))
                for s in data.get("stocks") or []]
        rows += [row("期貨", f.get("txEname") or f.get("txDate"), f.get("txDesc"), f.get("lot"), None,
                     f.get("weight"), f.get("txDate")) for f in data.get("futures") or []]
        rows += [row("債券", b.get("bondNo"), b.get("bondName"), b.get("faceValue"), b.get("marketValue"),
                     b.get("weight")) for b in data.get("bonds") or []]
        rows = [x for x in rows if x]
        return Holdings(ann, iso(pcf.get("date2")), SRC_PCF, rows) if rows else None


# --------------------------------------------------------------------------- #
class CTBC(Source):
    issuer, adapter_key = "中信", "中國信託"
    URL = "https://www.ctbcinvestments.com.tw/API/etf/Buyback"
    KINDS = {"STOCK": "股票", "FUTURE": "期貨", "BOND": "債券", "ETF": "ETF", "FUND": "基金", "OPTION": "選擇權"}
    SKIP = {"MARGIN", "CASH"}

    def __init__(self):
        super().__init__()
        self._selected = None

    def _data(self, code: str, day: str) -> dict:
        key = (code, day)
        if key not in self._cache:
            self.ad._ensure()
            fid = self.ad._map.get(code)
            if not fid:
                raise ValueError(f"中信清單無此代號 {code}")
            if self._selected != fid:
                self.ad._page.select_option("select.selects", fid)
                self.ad._page.wait_for_timeout(300)
                self._selected = fid
            self._cache[key] = self.ad._search(day.replace("-", ""))
        return self._cache[key]

    def summary(self, code, day):
        head = (self._data(code, day).get("Data") or [{}])[0]
        ann = iso(head.get("公告日"))
        if not ann:
            return None
        basket = next((v for k, v in head.items() if k.endswith("基數之受益權單位數")), None)
        return Summary(ann, ann, iso(head.get("淨值日期")), num(head.get("與前日已發行單位差異數")),
                       num(head.get("已發行受益權單位總數")), num(head.get("每受益權單位淨資產價值")),
                       num(head.get("基金淨資產價值")), num(basket), self.URL)

    def holdings(self, code, day):
        data = self._data(code, day)
        head = (data.get("Data") or [{}])[0]
        ann = iso(head.get("公告日"))
        if not ann:
            return None
        rows = []
        for sec in data.get("Detail") or []:
            c = str(sec.get("Code") or "")
            if c in self.SKIP:
                continue
            kind = self.KINDS.get(c) or str(sec.get("Name") or c)
            rows += [row(kind, x.get("code_"), x.get("name_"), x.get("qty_"), x.get("amount_"), x.get("weights_"),
                         x.get("ym_")) for x in sec.get("Data") or []]
        rows = [x for x in rows if x]
        return Holdings(ann, iso(head.get("淨值日期")), SRC_PCF, rows) if rows else None

    def close(self):
        self._selected = None
        super().close()


SOURCES: dict[str, type[Source]] = {c.issuer: c for c in (Yuanta, Cathay, Fubon, FuhHwa, President, Capital, CTBC)}
