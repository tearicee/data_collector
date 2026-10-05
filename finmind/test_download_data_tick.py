"""download_data_tick 的單元測試 (不連網)。

執行：cd finmind && ../.venv/bin/python -m unittest test_download_data_tick
"""
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

import download_data_tick as m

DS = "TaiwanFuturesTick"
THU, FRI, SAT, MON = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3), date(2026, 10, 5)


class FakeApi:
    """daily[交易日] = [(商品, 時段, 成交量)]；ticks[(日曆日, 商品)] = [時間字串]。"""

    def __init__(self, daily, ticks, fail=()):
        self.daily, self.ticks, self.fail = daily, ticks, set(fail)
        self.calls = []

    def __call__(self, params, label):
        self.calls.append(params)
        d = date.fromisoformat(params["start_date"])
        if params["dataset"] == "TaiwanFuturesDaily":
            return [{"date": str(d), "futures_id": i, "trading_session": s, "volume": v}
                    for i, s, v in self.daily.get(d, [])]
        key = (d, params["data_id"])          # 逐筆一定要帶 data_id，沒帶直接 KeyError
        if key in self.fail:
            raise m.ApiFail(f"{label} 模擬失敗")
        return [{"contract_date": "202610", "date": f"{d} {t}", "futures_id": key[1],
                 "price": 100.0, "volume": 2} for t in self.ticks.get(key, [])]

    def tick_ids(self, d=None):
        return [p["data_id"] for p in self.calls
                if p["dataset"] == DS and (d is None or p["start_date"] == str(d))]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._log, m.log = m.log, lambda msg: None

    def tearDown(self):
        m.log = self._log
        self._tmp.cleanup()

    def collector(self, api, today):
        return m.Collector(api, self.root, today)

    def read(self, d):
        return pd.read_parquet(m.out_path(DS, str(d), self.root))

    def marker(self, d):
        return m.partial_marker(m.out_path(DS, str(d), self.root))


class DownloadDayTest(Base):
    def test_same_day_is_partial_and_only_traded_ids_are_requested(self):
        api = FakeApi({FRI: [("TX", "position", 9), ("MTX", "after_market", 3), ("ZZF", "position", 0)]},
                      {(FRI, "TX"): ["09:00:00"], (FRI, "MTX"): ["01:00:00"]})
        self.assertEqual(self.collector(api, FRI).download_day(DS, FRI), "partial")
        self.assertEqual(api.tick_ids(), ["MTX", "TX"])           # 成交量 0 的 ZZF 不查
        self.assertTrue(self.marker(FRI).exists())
        self.assertEqual(list(self.read(FRI)["futures_id"]), ["MTX", "TX"])

    def test_partial_waits_until_next_trading_day_is_published(self):
        api = FakeApi({FRI: [("TX", "position", 9)]}, {(FRI, "TX"): ["09:00:00"]})
        self.collector(api, FRI).download_day(DS, FRI)
        api.calls.clear()
        self.assertEqual(self.collector(api, SAT).download_day(DS, FRI), "skip")
        self.assertEqual(api.tick_ids(), [])
        self.assertTrue(self.marker(FRI).exists())

    def test_finalize_refetches_only_evening_ids_and_keeps_the_rest(self):
        daily = {FRI: [("TX", "position", 9), ("CCF", "position", 5)]}
        ticks = {(FRI, "TX"): ["09:00:00"], (FRI, "CCF"): ["10:00:00"]}
        api = FakeApi(daily, ticks)
        self.collector(api, FRI).download_day(DS, FRI)

        daily[MON] = [("TX", "after_market", 4), ("TX", "position", 8), ("CCF", "position", 2)]
        ticks[(FRI, "TX")] = ["09:00:00", "15:00:01", "23:59:58"]
        api.calls.clear()
        self.assertEqual(self.collector(api, MON).download_day(DS, FRI), "ok")
        self.assertEqual(api.tick_ids(), ["TX"])                  # CCF 沒有夜盤，不重抓
        self.assertFalse(self.marker(FRI).exists())
        df = self.read(FRI)
        self.assertEqual(list(df["futures_id"]), ["CCF", "TX", "TX", "TX"])
        self.assertEqual(df["date"].iloc[-1], f"{FRI} 23:59:58")
        self.assertEqual(self.collector(api, MON).download_day(DS, FRI), "skip")

    def test_finalize_refuses_to_replace_existing_rows_with_empty_response(self):
        daily = {FRI: [("TX", "position", 9)]}
        ticks = {(FRI, "TX"): ["09:00:00"]}
        api = FakeApi(daily, ticks)
        self.collector(api, FRI).download_day(DS, FRI)
        daily[MON] = [("TX", "after_market", 4)]
        ticks[(FRI, "TX")] = []
        self.assertEqual(self.collector(api, MON).download_day(DS, FRI), "fail")
        self.assertEqual(len(self.read(FRI)), 1)
        self.assertTrue(self.marker(FRI).exists())

    def test_saturday_has_only_the_night_session_tail(self):
        api = FakeApi({MON: [("TX", "after_market", 4), ("CCF", "position", 2)]},
                      {(SAT, "TX"): ["00:00:01", "04:59:59"]})
        self.assertEqual(self.collector(api, SAT).download_day(DS, SAT), "nodata")   # 週一還沒出來
        self.assertEqual(api.tick_ids(), [])
        self.assertEqual(self.collector(api, MON).download_day(DS, SAT), "ok")
        self.assertEqual(api.tick_ids(), ["TX"])
        self.assertFalse(self.marker(SAT).exists())
        self.assertEqual(len(self.read(SAT)), 2)

    def test_past_day_is_downloaded_final_in_one_go(self):
        api = FakeApi({THU: [("TX", "position", 9)], FRI: [("TX", "after_market", 4), ("MTX", "after_market", 1)]},
                      {(THU, "TX"): ["09:00:00", "20:00:00"], (THU, "MTX"): ["21:00:00"]})
        self.assertEqual(self.collector(api, MON).download_day(DS, THU), "ok")
        self.assertEqual(api.tick_ids(), ["MTX", "TX"])           # MTX 只有夜盤成交，也要抓
        self.assertFalse(self.marker(THU).exists())

    def test_existing_file_without_marker_is_skipped_without_requests(self):
        dest = m.out_path(DS, str(FRI), self.root)
        dest.parent.mkdir(parents=True)
        pd.DataFrame({"futures_id": ["TX"], "date": [f"{FRI} 09:00:00"]}).to_parquet(dest)
        api = FakeApi({}, {})
        self.assertEqual(self.collector(api, MON).download_day(DS, FRI), "skip")
        self.assertEqual(api.calls, [])

    def test_one_failed_id_fails_the_day_and_writes_nothing(self):
        api = FakeApi({FRI: [("TX", "position", 9), ("MTX", "position", 3)]},
                      {(FRI, "TX"): ["09:00:00"], (FRI, "MTX"): ["09:00:00"]}, fail=[(FRI, "TX")])
        self.assertEqual(self.collector(api, FRI).download_day(DS, FRI), "fail")
        self.assertFalse(m.out_path(DS, str(FRI), self.root).exists())
        self.assertFalse(self.marker(FRI).exists())


class RunTest(Base):
    def test_exit_code_and_pending_days_outside_window(self):
        daily = {FRI: [("TX", "position", 9)]}
        ticks = {(FRI, "TX"): ["09:00:00"]}
        api = FakeApi(daily, ticks)
        self.assertEqual(m.run(self.collector(api, FRI), [DS], FRI, FRI, True), 0)
        self.assertEqual(m.pending_days(DS, self.root), [FRI])

        # 之後的執行視窗不含週五，掛著標記的日子仍會被補成定稿
        daily[MON] = [("TX", "after_market", 4), ("TX", "position", 8)]
        ticks[(FRI, "TX")] = ["09:00:00", "15:00:01"]
        ticks[(MON, "TX")] = ["09:00:00"]
        self.assertEqual(m.run(self.collector(api, MON), [DS], MON, MON, True), 0)
        self.assertEqual(m.pending_days(DS, self.root), [MON])
        self.assertEqual(len(self.read(FRI)), 2)

    def test_failures_give_nonzero_exit_and_circuit_breaker(self):
        days = [date(2026, 9, d) for d in (14, 15, 16, 17, 18)]      # 週一~週五
        api = FakeApi({d: [("TX", "position", 9)] for d in days}, {}, fail=[(d, "TX") for d in days])
        self.assertEqual(m.run(self.collector(api, days[0]), [DS], days[0], days[0], False), 1)
        limit, m.MAX_CONSECUTIVE_FAILURES = m.MAX_CONSECUTIVE_FAILURES, 3
        try:
            api.calls.clear()
            self.assertEqual(m.run(self.collector(api, days[-1]), [DS], days[0], days[-1], False), 3)
            self.assertEqual(api.tick_ids(), ["TX"] * 3)             # 熔斷後不再打 API
        finally:
            m.MAX_CONSECUTIVE_FAILURES = limit

if __name__ == "__main__":
    unittest.main()
