"""群益 adapter 的 post 重試 (不連網)。執行：cd raw_holdings && ../../.venv/bin/python -m unittest test_capital_post"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "issuers"))
import capital  # noqa: E402


class TimeoutError_(Exception):          # playwright 的 TimeoutError 只能靠類別名判斷
    pass


class FakeResp:
    def __init__(self, payload: bytes):
        self._b = payload

    def body(self):
        return self._b


class FakePage:
    def __init__(self, fail_times: int, exc=TimeoutError_):
        self.calls = 0
        self.fail_times = fail_times
        self.exc = exc
        self.request = self

    def post(self, url, data=None, timeout=None, headers=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc("Timeout 30000ms exceeded.\n=== Call log ===\n  - cookie: secret")
        return FakeResp(b'{"data": {"ok": 1}}')


class PostRetryTest(unittest.TestCase):
    def setUp(self):
        capital.RETRY_WAIT_S = 0
        self.ad = capital.CapitalAdapter("群益")

    def test_retries_timeout_then_succeeds(self):
        self.ad._page = FakePage(fail_times=2)
        self.assertEqual(self.ad.post("u", {}), {"data": {"ok": 1}})
        self.assertEqual(self.ad._page.calls, 3)

    def test_gives_up_after_retries(self):
        self.ad._page = FakePage(fail_times=3)
        with self.assertRaises(TimeoutError_):
            self.ad.post("u", {})
        self.assertEqual(self.ad._page.calls, 3)

    def test_other_errors_are_not_retried(self):
        self.ad._page = FakePage(fail_times=1, exc=ValueError)
        with self.assertRaises(ValueError):
            self.ad.post("u", {})
        self.assertEqual(self.ad._page.calls, 1)


if __name__ == "__main__":
    unittest.main()
