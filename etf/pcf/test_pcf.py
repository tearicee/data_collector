#!/usr/bin/env python3
"""pcf 資料集的測試 (不連網)。執行: cd etf && python -m unittest pcf.test_pcf -v"""
import io
import sys
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect  # noqa: E402
import import_history  # noqa: E402
import sources  # noqa: E402
import store  # noqa: E402
from sources import Holdings, Summary  # noqa: E402

DAYS = ["2026-09-24", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"]      # 09-25、09-28 休市
CAL = store.Calendar(DAYS)


class AsofRuleTest(unittest.TestCase):
    """「交易日」→「資料基準日」的規則 (依據見 store.py)。"""

    def test_summary_pcf_date_issuers_are_previous_trading_day(self):
        for issuer in ("元大", "國泰", "復華", "統一", "群益", "中信"):
            self.assertEqual(store.summary_asof(issuer, "國內", "2026-09-29", CAL), "2026-09-24", issuer)

    def test_summary_fubon_is_same_day(self):
        self.assertEqual(store.summary_asof("富邦", "國內", "2026-09-29", CAL), "2026-09-29")

    def test_constituents_split_by_issuer_and_source(self):
        for issuer in ("國泰", "富邦", "復華"):
            self.assertEqual(store.constituent_asof(issuer, "國內", "持股明細", "2026-09-30", CAL), "2026-09-30")
        for issuer in ("元大", "統一", "群益", "中信"):
            self.assertEqual(store.constituent_asof(issuer, "國內", "持股明細", "2026-09-30", CAL), "2026-09-29")
        # 以申購買回清單補的日子，即使是國泰也是公告日
        self.assertEqual(store.constituent_asof("國泰", "國內", "申購買回清單(每申購基數)", "2026-09-30", CAL),
                         "2026-09-29")

    def test_overseas_and_bond_are_not_inferred(self):
        for market in ("海外", "債券"):
            self.assertIsNone(store.summary_asof("元大", market, "2026-09-30", CAL))
            self.assertIsNone(store.constituent_asof("富邦", market, "持股明細", "2026-09-30", CAL))

    def test_prev_works_for_a_day_not_yet_in_calendar(self):
        self.assertEqual(CAL.prev("2026-10-05"), "2026-10-02")          # 下一個公告日還沒有收盤價
        self.assertIsNone(CAL.prev("2026-09-24"))


class ParseHelpersTest(unittest.TestCase):
    def test_num(self):
        self.assertEqual(sources.num("NT$475,950,467,267"), 475950467267.0)
        self.assertEqual(sources.num("3.966%"), 3.966)
        self.assertEqual(sources.num("TWD 2,153,615,171.00"), 2153615171.0)
        for empty in (None, "", "-", "N/A", float("nan")):
            self.assertIsNone(sources.num(empty))

    def test_iso(self):
        self.assertEqual(sources.iso("20260930"), "2026-09-30")
        self.assertEqual(sources.iso("2026/9/30 上午 12:00:00"), "2026-09-30")
        self.assertIsNone(sources.iso("1150929"))

    def test_net_date_handles_both_serializations(self):
        # 統一的伺服器兩種寫法交替出現；毫秒是 UTC，要換成台灣日期
        self.assertEqual(sources.net_date("/Date(1790611200000)/"), "2026-09-29")
        self.assertEqual(sources.net_date("2026-09-29T00:00:00"), "2026-09-29")
        self.assertIsNone(sources.net_date("/Date(-62135596800000)/"))

    def test_row_drops_blank_codes_and_normalizes(self):
        self.assertIsNone(sources.row("股票", "  ", "x", 1))
        r = sources.row("期貨", " TX ", "台指期貨", "679", "6,488,659,800", "2.29", "")
        self.assertEqual((r["成分代碼"], r["數量"], r["金額"], r["權重(%)"], r["到期月份"]),
                         ("TX", 679.0, 6488659800.0, 2.29, None))


def make_zip(path: Path):
    """仿 2026-10-02 歷史壓縮檔的結構: 申購贖回 CSV + 巢狀成分股 zip (含一檔拆成兩部分)。"""
    summary = pd.DataFrame([
        [1, "0050", "元大台灣卓越50", "元大", "2026-09-29", "2026-09-29", "有資料", 27000000, 22078000000, 112.33, 2.48e12, 500000, "u"],
        [1, "0050", "元大台灣卓越50", "元大", "2026-09-30", "2026-09-30", "有資料", 37500000, 22115500000, 111.24, 2.46e12, 500000, "u"],
        [5, "006208", "富邦台50", "富邦", "2026-09-30", "2026-10-01", "有資料", 0, 1860540000, 255.81, 4.76e11, 500000, "u"],
        [10, "00679B", "元大美債20年", "元大", "2026-09-30", "2026-09-30", "有資料", 0, 1, 24.99, 1e9, 500000, "u"],
        [1, "0050", "元大台灣卓越50", "元大", "2026-09-24", None, "來源無資料", None, None, None, None, None, None],
    ], columns=["市值排名", "代號", "名稱", "投信", "交易日", "公告日期", "狀態", "申購贖回淨增減單位數",
                "已發行單位數", "每單位淨值", "基金淨資產", "實物申贖基數", "來源網址"])

    def const(code, issuer, day, source="持股明細"):
        return pd.DataFrame([[code, "n", issuer, day, source, "股票", "2330", "台積電", 1000, None, 56.5, None]],
                            columns=["ETF代號", "ETF名稱", "投信", "交易日", "資料來源", "類別", "成分代碼",
                                     "成分名稱", "數量", "金額", "權重(%)", "到期月份"])
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zi:
        zi.writestr("前50大ETF每日成分股/00_各檔覆蓋.csv", "x\n")
        zi.writestr("前50大ETF每日成分股/01_0050_每日成分股.csv",
                    pd.concat([const("0050", "元大", "2026-09-29"), const("0050", "元大", "2026-09-30")]).to_csv(index=False))
        zi.writestr("前50大ETF每日成分股/05_006208_每日成分股_第1部分_2026-09-29至2026-09-29.csv",
                    const("006208", "富邦", "2026-09-29").to_csv(index=False))
        zi.writestr("前50大ETF每日成分股/05_006208_每日成分股_第2部分_2026-09-30至2026-09-30.csv",
                    const("006208", "富邦", "2026-09-30").to_csv(index=False))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("資料夾/前50大ETF每日申購贖回.csv", summary.to_csv(index=False))
        z.writestr("資料夾/前50大ETF每日成分股.zip", inner.getvalue())
        z.writestr("資料夾/前50大ETF每日成分股_各檔覆蓋.csv", "x\n")


class ImportHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "store"
        self.zip = Path(self.tmp.name) / "hist.zip"
        make_zip(self.zip)
        self.empty_prices = Path(self.tmp.name) / "no_prices"

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_keeps_original_values_and_adds_asof(self):
        stats = import_history.import_zip(self.zip, self.root, self.empty_prices, verbose=False)
        self.assertEqual((stats["summary_from_zip"], stats["constituent_files"], stats["constituent_rows"]), (5, 2, 4))
        s = store.load_summary(self.root).set_index(["代號", "交易日"])
        self.assertEqual(s.loc[("0050", "2026-09-30"), "資料基準日"], "2026-09-29")       # 元大: 公告日 → 前一交易日
        self.assertEqual(s.loc[("006208", "2026-09-30"), "資料基準日"], "2026-09-30")     # 富邦: 同日
        self.assertEqual(s.loc[("0050", "2026-09-30"), "基準日依據"], store.BY_RULE)
        self.assertTrue(pd.isna(s.loc[("00679B", "2026-09-30"), "資料基準日"]))            # 債券型不推定
        self.assertTrue(pd.isna(s.loc[("0050", "2026-09-24"), "資料基準日"]))              # 來源無資料的列
        self.assertEqual(s.loc[("0050", "2026-09-30"), "已發行單位數"], 22115500000)
        c = store.load_constituents("0050", self.root)
        self.assertEqual(list(c["資料基準日"]), ["2026-09-24", "2026-09-29"])
        self.assertEqual(import_history.check(self.zip, self.root), 0)

    def test_split_part_files_are_merged(self):
        import_history.import_zip(self.zip, self.root, self.empty_prices, verbose=False)
        self.assertEqual(list(store.load_constituents("006208", self.root)["交易日"]), ["2026-09-29", "2026-09-30"])

    def test_reimport_does_not_overwrite_collected_rows(self):
        import_history.import_zip(self.zip, self.root, self.empty_prices, verbose=False)
        newer = store.load_summary(self.root).query("代號 == '0050' and 交易日 == '2026-09-30'").copy()
        newer["每單位淨值"], newer["基準日依據"] = 999.0, store.BY_SOURCE
        store.upsert_summary(newer, self.root)
        extra = store.load_constituents("0050", self.root).tail(1).assign(交易日="2026-10-01")
        store.upsert_constituents("0050", extra, self.root)
        import_history.import_zip(self.zip, self.root, self.empty_prices, verbose=False)
        s = store.load_summary(self.root).set_index(["代號", "交易日"])
        self.assertEqual(s.loc[("0050", "2026-09-30"), "每單位淨值"], 999.0)
        self.assertIn("2026-10-01", set(store.load_constituents("0050", self.root)["交易日"]))


class StoreUpsertTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _const(self, day, qty):
        return pd.DataFrame([{"ETF代號": "0050", "交易日": day, "類別": "股票", "成分代碼": "2330", "數量": qty}])

    def test_constituents_replace_whole_day_and_report_changes(self):
        self.assertEqual(store.upsert_constituents("0050", self._const("2026-09-30", 1), self.root), 1)
        self.assertEqual(store.upsert_constituents("0050", self._const("2026-09-30", 1), self.root), 0)   # 內容相同
        self.assertEqual(store.upsert_constituents("0050", self._const("2026-09-30", 2), self.root), 1)
        c = store.load_constituents("0050", self.root)
        self.assertEqual((len(c), c["數量"].iloc[0]), (1, 2.0))

    def test_summary_upsert_is_keyed_by_code_and_day(self):
        r = {"市值排名": 1, "代號": "0050", "交易日": "2026-09-30", "狀態": store.HAS_DATA, "每單位淨值": 1.0}
        self.assertEqual(store.upsert_summary(pd.DataFrame([r]), self.root), 1)
        self.assertEqual(store.upsert_summary(pd.DataFrame([r]), self.root), 0)
        self.assertEqual(store.upsert_summary(pd.DataFrame([dict(r, 每單位淨值=2.0)]), self.root), 1)
        self.assertEqual(len(store.load_summary(self.root)), 1)


class FakeSource(sources.Source):
    """以 {交易日: 資料} 模擬投信網站；記錄被查了哪些日子。"""
    issuer = "元大"
    data: dict = {}
    calls: list = []

    def __init__(self):
        self._cache = {}

    def summary(self, code, day):
        type(self).calls.append(("s", code, day))
        if day not in self.data:
            return None
        return Summary(day, day, CAL.prev(day), 0.0, 100.0, 10.0, 1000.0, 500000.0, "url")

    def holdings(self, code, day):
        type(self).calls.append(("h", code, day))
        if day not in self.data:
            return None
        return Holdings(day, CAL.prev(day), sources.SRC_HOLDINGS, [sources.row("股票", "2330", "台積電", 1, None, 50)])

    def close(self):
        pass


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._universe, self._price_days = store.load_universe, store.price_days
        store.load_universe = lambda: pd.DataFrame(
            [{"市值排名": "1", "代號": "0050", "名稱": "元大台灣卓越50", "投信": "元大", "市場": "國內"}])
        store.price_days = lambda price_dir=None: [d for d in DAYS if d <= "2026-10-02"]
        FakeSource.calls = []

    def tearDown(self):
        store.load_universe, store.price_days = self._universe, self._price_days
        self.tmp.cleanup()

    def run_collect(self, data, today="2026-10-02", **kw):
        FakeSource.data = data
        return collect.collect(self.root, today=date.fromisoformat(today), sleep=0,
                               sources={"元大": FakeSource}, verbose=False, **kw)

    def test_fills_recent_gaps_and_takes_next_trading_days_list(self):
        # 10/02 (五) 晚上: 補 9/29~10/02，並抓到公告日 10/05 (一) 的清單就停
        stats = self.run_collect({d: 1 for d in DAYS + ["2026-10-05"]})
        s = store.load_summary(self.root)
        self.assertEqual(list(s["交易日"]), ["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05"])
        self.assertEqual(stats["summary_new"], 5)
        row = s.set_index("交易日").loc["2026-10-05"]
        self.assertEqual((row["資料基準日"], row["基準日依據"]), ("2026-10-02", store.BY_SOURCE))
        self.assertEqual(sorted(store.load_constituents("0050", self.root)["交易日"].unique())[-1], "2026-10-05")
        self.assertNotIn(("s", "0050", "2026-10-06"), FakeSource.calls)       # 找到下一個交易日就不再往後

    def test_second_run_queries_only_what_is_still_missing(self):
        self.run_collect({d: 1 for d in DAYS + ["2026-10-05"]})
        FakeSource.calls = []
        stats = self.run_collect({d: 1 for d in DAYS + ["2026-10-05"]})
        self.assertEqual((stats["summary_new"], stats["constituent_days_new"], FakeSource.calls), (0, 0, []))

    def test_days_without_data_are_not_stored_and_get_retried(self):
        self.run_collect({"2026-09-30": 1})                                   # 10/01、10/02 尚未公告
        self.assertEqual(list(store.load_summary(self.root)["交易日"]), ["2026-09-30"])
        FakeSource.calls = []
        self.run_collect({"2026-09-30": 1, "2026-10-01": 1})
        self.assertIn(("s", "0050", "2026-10-01"), FakeSource.calls)
        self.assertNotIn(("s", "0050", "2026-09-30"), FakeSource.calls)
        self.assertEqual(list(store.load_summary(self.root)["交易日"]), ["2026-09-30", "2026-10-01"])

    def test_forward_search_gives_up_after_two_empty_weekdays(self):
        self.run_collect({d: 1 for d in DAYS})                                # 下一個交易日的清單還沒公告
        forward = [d for k, _, d in FakeSource.calls if k == "s" and d > "2026-10-02"]
        self.assertEqual(forward, ["2026-10-05", "2026-10-06"])

    def test_forward_search_skips_a_holiday(self):
        self.run_collect({d: 1 for d in DAYS + ["2026-10-06"]})               # 10/05 休市，清單公告日是 10/06
        self.assertEqual(store.load_summary(self.root)["交易日"].max(), "2026-10-06")

    def test_dry_run_writes_nothing(self):
        stats = self.run_collect({d: 1 for d in DAYS}, dry_run=True)
        self.assertGreater(stats["summary_new"], 0)
        self.assertFalse(store.summary_path(self.root).exists())

    def test_one_etf_failing_is_reported_not_fatal(self):
        class Broken(FakeSource):
            def summary(self, code, day):
                raise RuntimeError("網站掛了")
        FakeSource.data = {}
        stats = collect.collect(self.root, today=date(2026, 10, 2), sleep=0, sources={"元大": Broken}, verbose=False)
        self.assertEqual(stats["failed"], ["0050"])

    def test_explicit_range_backfills_only_that_range(self):
        self.run_collect({d: 1 for d in DAYS}, since="2026-09-29", until="2026-09-30")
        self.assertEqual(list(store.load_summary(self.root)["交易日"]), ["2026-09-29", "2026-09-30"])


if __name__ == "__main__":
    unittest.main()
