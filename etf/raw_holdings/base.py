#!/usr/bin/env python3
"""投信原始持股爬蟲 — Adapter 基底與註冊表。

每家投信官網的 ETF 每日持股 (PCF / 持股明細) 公告格式不同，故以「一家一個
adapter」的方式擴充。所有 adapter 繼承 IssuerAdapter 並用 @register 註冊，
輸出統一正規化欄位:

    date       資料日期 (YYYYMMDD)
    fund_code  基金代號
    stock_id   成分股代號
    name       成分股名稱
    shares     持有股數 / 張數 (依來源，於欄位說明註明)
    weight     權重(%)

新增一家投信:
  1. 在 issuers/ 新增 <issuer>.py，定義子類別覆寫 fetch()
  2. 用 @register("發行商中文短名") 裝飾
  3. 於 issuers/__init__.py import 該模組 (觸發註冊)
"""
from __future__ import annotations
import random
from abc import ABC, abstractmethod

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

NORMALIZED_COLUMNS = ["date", "fund_code", "stock_id", "name", "asset_type",
                      "shares", "weight"]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
]

# 發行商中文短名 → adapter 實例
REGISTRY: dict[str, "IssuerAdapter"] = {}


class NotSupportedYet(Exception):
    """該發行商尚未實作 adapter (框架已就緒，待逐家踩點)。"""


def register(issuer: str):
    """類別裝飾器：註冊某發行商的 adapter。"""
    def deco(cls):
        REGISTRY[issuer] = cls(issuer)
        return cls
    return deco


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=3, connect=3, read=3, status=3, backoff_factor=1,
                  status_forcelist=[429, 500, 502, 503, 504],
                  respect_retry_after_header=True)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class IssuerAdapter(ABC):
    """單一投信的持股 adapter 基底。"""

    def __init__(self, issuer: str):
        self.issuer = issuer
        self.session = build_session()

    def headers(self) -> dict:
        return {"User-Agent": random.choice(USER_AGENTS)}

    @abstractmethod
    def fetch(self, fund_code: str, date: str) -> pd.DataFrame:
        """抓取單一基金某日持股，回傳含 NORMALIZED_COLUMNS 的 DataFrame。

        date: YYYYMMDD (預期交易日)。查無資料應回空 DataFrame 或拋
        ValueError；尚未實作應拋 NotSupportedYet。
        """
        raise NotImplementedError

    def close(self):
        """釋放資源 (如 headless browser)。預設 no-op；run_raw 結束時呼叫。"""
        pass

    def normalize(self, df: pd.DataFrame, fund_code: str, date: str) -> pd.DataFrame:
        """補齊 date/fund_code 欄並統一欄位順序。

        adapter 若已在 df 填入 date (來源實際交易日) / fund_code，則尊重之，
        不覆寫；缺漏才補上傳入值。
        """
        df = df.copy()
        if "date" not in df.columns:
            df["date"] = date
        if "fund_code" not in df.columns:
            df["fund_code"] = fund_code
        for col in NORMALIZED_COLUMNS:
            if col not in df.columns:
                df[col] = pd.NA
        return df[NORMALIZED_COLUMNS]


class StubAdapter(IssuerAdapter):
    """尚未實作的發行商佔位 adapter：一律拋 NotSupportedYet。"""

    def fetch(self, fund_code: str, date: str) -> pd.DataFrame:
        raise NotSupportedYet(f"{self.issuer} 尚未實作 adapter")
