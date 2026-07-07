#!/usr/bin/env python3
"""統一投信 (President, PSIT) 原始持股 adapter — requests (免瀏覽器)。

統一 www.ezmoney.com.tw 是 ASP.NET，requests session 帶 cookie 即可過 302:
  1. GET /ETF (建立 session cookie)
  2. GET /ETF/Transaction/PCF → a href fundCode=<內部碼> + 文字代號，建
     交易所代號→FundCode 映射 (如 00939→46YTW)
  3. GET /ETF/Fund/AssetExcelNPOI?fundCode=<內部碼> → 下載持股 Excel(.xlsx)

Excel 多區塊(各有表頭)，資料日為民國(如 115/07/06):
  股票: 股票代號/股票名稱/股數/持股權重
  期貨: 期貨代號/期貨名稱/持股權重/口數/契約年月
  債券: 債券代號/...(比照，依表頭欄位判斷)
依表頭首欄(『X代號』)判 asset_type，權重欄含『權重』、數量欄含股數/口數/面額。
"""
from __future__ import annotations

import io
import re

import pandas as pd

from base import IssuerAdapter, register

BASE = "https://www.ezmoney.com.tw"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0",
      "Accept-Language": "zh-TW"}
CODE_RE = re.compile(r"^\d{4,6}[A-Z]?$")


def _num(v):
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "nan", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _pct(v):
    if v is None:
        return None
    s = str(v).replace("%", "").replace(",", "").strip()
    if s in ("", "-", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _roc_to_date(text: str) -> str | None:
    """民國『資料日期：115/07/06』→ 20260706。"""
    m = re.search(r"(\d{2,3})/(\d{1,2})/(\d{1,2})", str(text))
    if not m:
        return None
    y = int(m.group(1)) + 1911
    return f"{y:04d}{int(m.group(2)):02d}{int(m.group(3)):02d}"


@register("統一")
class PresidentAdapter(IssuerAdapter):

    def __init__(self, issuer: str):
        super().__init__(issuer)
        self._map: dict[str, str] | None = None

    def _ensure(self):
        if self._map is not None:
            return
        self.session.get(f"{BASE}/ETF", headers=UA, timeout=(10, 30))  # cookie
        html = self.session.get(f"{BASE}/ETF/Transaction/PCF", headers=UA,
                                timeout=(10, 30)).text
        m = {}
        for a in re.finditer(r'href="[^"]*[Ff]und[Cc]ode=([^"&]+)[^"]*"[^>]*>([^<]*)', html):
            fc, txt = a.group(1).strip(), a.group(2).strip()
            code = txt.split()[0] if txt else ""
            if CODE_RE.match(code):
                m.setdefault(code, fc)
        self._map = m

    def fetch(self, fund_code: str, date: str) -> pd.DataFrame:
        self._ensure()
        fc = self._map.get(fund_code)
        if not fc:
            raise ValueError(f"統一清單無此代號 {fund_code}")

        r = self.session.get(f"{BASE}/ETF/Fund/AssetExcelNPOI",
                             params={"fundCode": fc}, headers=UA, timeout=(10, 30))
        r.raise_for_status()
        df = pd.read_excel(io.BytesIO(r.content), header=None)

        data_date = _roc_to_date(df.iloc[0, 0]) or date
        rows = self._parse(df)
        if not rows:
            raise ValueError(f"統一 {fund_code} 無持股明細")

        out = pd.DataFrame(rows)
        out["date"] = data_date
        out["fund_code"] = fund_code
        return out

    @staticmethod
    def _parse(df: pd.DataFrame) -> list[dict]:
        rows: list[dict] = []
        mode = None            # 目前資產類別 (股票/期貨/債券…)
        wi = qi = None         # 權重欄 / 數量欄 index
        for _, row in df.iterrows():
            cells = [("" if pd.isna(x) else str(x).strip()) for x in row]
            c0 = cells[0]
            # 區塊表頭: 首欄以『代號』結尾 (股票代號/期貨代號/債券代號)
            if c0.endswith("代號"):
                mode = c0.replace("代號", "").strip() or "其他"
                wi = next((j for j, h in enumerate(cells) if "權重" in h), None)
                qi = next((j for j, h in enumerate(cells)
                           if any(k in h for k in ["股數", "口數", "面額", "數量"])), None)
                continue
            if not mode:
                continue
            # 區塊結束: 空列或摘要關鍵字
            if not c0 or any(k in c0 for k in ["項目", "基金", "資料日期", "淨資產", "合計"]):
                mode = None
                continue
            name = cells[1] if len(cells) > 1 else ""
            rows.append({
                "stock_id": c0,
                "name": name,
                "asset_type": mode,
                "shares": _num(cells[qi]) if qi is not None and qi < len(cells) else None,
                "weight": _pct(cells[wi]) if wi is not None and wi < len(cells) else None,
            })
        return rows
