#!/usr/bin/env python3
"""to_cmoney 的測試 (合成資料)。執行: cd etf && python -m unittest raw_holdings.test_to_cmoney -v"""
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import to_cmoney as T  # noqa: E402

STOCKS = [f"{2300 + i}" for i in range(12)]
SHARES = [1_000_000 * (i + 1) for i in range(12)]
# 三個交易日的收盤價；各股每天漲跌幅不同，日期才分得出來
PRICES = {
    "20261001": [100 + 3 * i for i in range(12)],
    "20261002": [(100 + 3 * i) * (1 + 0.02 * ((i % 5) - 2)) for i in range(12)],
    "20261005": [(100 + 3 * i) * (1 + 0.03 * ((i % 3) - 1)) for i in range(12)],
}


def raw_frame(day: str, etf: str = "00999A", label: str | None = None, extra: bool = True) -> pd.DataFrame:
    """以 day 的收盤價算權重的投信格式持股；label 是檔內 date 欄 (可與真實基準日不同)。"""
    mv = [s * p for s, p in zip(SHARES, PRICES[day])]
    total = sum(mv) / 0.95                                # 個股佔淨值 95%
    rows = [{"date": label or day, "fund_code": etf, "stock_id": sid, "name": sid, "asset_type": "股票",
             "shares": float(sh), "weight": round(m / total * 100, 4)}
            for sid, sh, m in zip(STOCKS, SHARES, mv)]
    if extra:                                             # 期貨與海外標的列
        rows.append({"date": label or day, "fund_code": etf, "stock_id": "TX", "name": "臺股期貨",
                     "asset_type": "期貨", "shares": 12.0, "weight": 1.5})
        rows.append({"date": label or day, "fund_code": etf, "stock_id": "NVDA US", "name": "NVIDIA",
                     "asset_type": "股票", "shares": 5000.0, "weight": 2.0})
    return pd.DataFrame(rows)


class Env:
    """暫存的 raw / out / price 目錄。"""

    def __init__(self, price_days=("20261001", "20261002", "20261005")):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.raw, self.out, self.price = root / "raw", root / "out", root / "price"
        self.out.mkdir()
        self._min_rows = T.MIN_PRICE_ROWS
        T.MIN_PRICE_ROWS = 5
        for d in price_days:
            ydir = self.price / d[:4]
            ydir.mkdir(parents=True, exist_ok=True)
            pd.DataFrame({"stock_id": STOCKS, "close": PRICES[d]}).to_parquet(
                ydir / f"TaiwanStockPrice_{d[:4]}-{d[4:6]}-{d[6:]}.parquet")

    def put(self, issuer: str, label: str, etf: str, df: pd.DataFrame) -> Path:
        (self.raw / issuer).mkdir(parents=True, exist_ok=True)
        p = self.raw / issuer / f"{label}_{etf}.csv"
        df.to_csv(p, index=False, encoding="utf-8-sig")
        return p

    def convert(self, **kw):
        return T.convert(self.raw, self.out, self.price, "20260101", verbose=False, **kw)

    def close(self):
        T.MIN_PRICE_ROWS = self._min_rows
        self.tmp.cleanup()


class ValuationDateTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.prices = T.load_prices(self.env.price)

    def tearDown(self):
        self.env.close()

    def test_picks_day_whose_prices_reproduce_weights(self):
        for day in PRICES:
            fit = T.valuation_date(T.read_raw(self.env.put("X", "20261005", "A", raw_frame(day))),
                                   "20261005", self.prices)
            self.assertEqual((fit.date, fit.reason), (day, "ok"))

    def test_label_does_not_decide_the_date(self):
        # 檔名標 10/05 (下一交易日 / 過午夜才跑)，內容是 10/02 的持股
        fit = T.valuation_date(raw_frame("20261002", label="20261005"), "20261005", self.prices)
        self.assertEqual(fit.date, "20261002")

    def test_candidates_never_after_label(self):
        fit = T.valuation_date(raw_frame("20261005"), "20261002", self.prices)
        self.assertIsNone(fit.date)                       # 真正的日子在檔名日期之後 → 不採信
        self.assertEqual(fit.reason, "poor_fit")

    def test_few_domestic_stocks_is_not_judged(self):
        df = raw_frame("20261002").head(T.MIN_STOCKS - 1)
        self.assertEqual(T.valuation_date(df, "20261002", self.prices).reason, "few_stocks")

    def test_unknown_tickers_have_no_price(self):
        df = raw_frame("20261002", extra=False)
        df["stock_id"] = [f"{9000 + i}" for i in range(len(df))]
        self.assertEqual(T.valuation_date(df, "20261002", self.prices).reason, "no_price")


class ConvertTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def read(self, name: str) -> pd.DataFrame:
        return pd.read_csv(self.env.out / name, dtype={"stock_id": str, "etf": str}, encoding="utf-8-sig")

    def test_output_matches_cmoney_format_and_units(self):
        self.env.put("富邦", "20261003", "00999A", raw_frame("20261002", label="20261003"))
        result = self.env.convert()
        self.assertEqual(result["per_date"], {"20261002": 1})
        out = self.read("20261002_00999A.csv")            # 以內容判定的基準日命名，不是檔名的 10/03
        self.assertEqual(list(out.columns), T.CMONEY_COLUMNS)
        self.assertTrue((out["date"] == 20261002).all() and (out["etf"] == "00999A").all())
        row = out[out["stock_id"] == STOCKS[0]].iloc[0]
        self.assertEqual(row["holdings"], SHARES[0] / 1000)             # 股 → 張
        self.assertEqual(row["weight(%)"], round(row["weight(%)"], 2))  # 權重 2 位小數
        text = (self.env.out / "20261002_00999A.csv").read_text(encoding="utf-8-sig")
        self.assertIn(",TX,1.50,0.012,", text)                          # 固定兩位小數；口數也 /1000
        self.assertIn("TX", set(out["stock_id"]))                       # 非個股列照留
        self.assertIn("NVDA US", set(out["stock_id"]))

    def test_cmoney_era_dates_are_never_written(self):
        old = T.CMONEY_LAST_DATE
        T.CMONEY_LAST_DATE = "20261001"
        try:
            self.env.put("元大", "20261001", "0050", raw_frame("20261001", etf="0050"))
            result = self.env.convert()
        finally:
            T.CMONEY_LAST_DATE = old
        self.assertEqual(result["skipped"], {"cmoney_era": 1})
        self.assertEqual(list(self.env.out.iterdir()), [])

    def test_same_date_twice_keeps_latest_fetch_and_is_idempotent(self):
        a = self.env.put("台新", "20261002", "00987A", raw_frame("20261002", etf="00987A"))
        b = self.env.put("台新", "20261003", "00987A", raw_frame("20261002", etf="00987A", label="20261003"))
        import os
        os.utime(a, (1, 1))
        os.utime(b, (2, 2))
        self.assertEqual(self.env.convert()["written"], 1)
        again = self.env.convert()
        self.assertEqual((again["written"], again["unchanged"]), (0, 1))

    def test_unjudgeable_files_are_skipped_not_guessed(self):
        self.env.put("元大", "20261002", "00632R", raw_frame("20261002").tail(2))   # 只有期貨/海外列
        result = self.env.convert()
        self.assertEqual((result["per_date"], result["skipped"]), ({}, {"few_stocks": 1}))

    def test_dry_run_writes_nothing(self):
        self.env.put("元大", "20261002", "0050", raw_frame("20261002", etf="0050"))
        self.assertEqual(self.env.convert(dry_run=True)["per_date"], {"20261002": 1})
        self.assertEqual(list(self.env.out.iterdir()), [])


class MissingPriceGuardTest(unittest.TestCase):
    def test_aborts_when_price_file_for_newest_holdings_is_missing(self):
        env = Env(price_days=("20261001", "20261002"))    # 沒有 10/05 的價檔
        try:
            env.put("元大", "20261005", "0050", raw_frame("20261005", etf="0050"))
            with self.assertRaisesRegex(RuntimeError, "收盤價只到 20261002"):
                env.convert()
            self.assertEqual(list(env.out.iterdir()), [])
            # 標「下一交易日」的發行商出現較新的檔名日期是正常的，不擋
            env2 = Env(price_days=("20261001", "20261002"))
            try:
                env2.put("群益", "20261005", "00919", raw_frame("20261002", etf="00919", label="20261005"))
                self.assertEqual(env2.convert()["per_date"], {"20261002": 1})
            finally:
                env2.close()
        finally:
            env.close()


if __name__ == "__main__":
    unittest.main()
