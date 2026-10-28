"""國泰 adapter 的 403 重試 (不連網)。執行：cd raw_holdings && ../../.venv/bin/python -m unittest test_cathay_403"""
import sys
import unittest
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "issuers"))
import cathay  # noqa: E402


class FakeResp:
    def __init__(self, status: int, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error")

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, statuses):
        self.statuses, self.calls = list(statuses), 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls += 1
        return FakeResp(self.statuses.pop(0), {"returnCode": "2000"})


class Cathay403Test(unittest.TestCase):
    def setUp(self):
        cathay.FORBIDDEN_WAIT_S = 0
        self.ad = cathay.CathayAdapter("國泰")

    def test_retries_403_then_succeeds(self):
        self.ad.session = FakeSession([403, 403, 200])
        self.assertEqual(self.ad.get_json("u", {}), {"returnCode": "2000"})
        self.assertEqual(self.ad.session.calls, 3)

    def test_gives_up_after_retries(self):
        self.ad.session = FakeSession([403, 403, 403])
        with self.assertRaises(requests.HTTPError):
            self.ad.get_json("u", {})
        self.assertEqual(self.ad.session.calls, 3)

    def test_other_http_errors_are_not_retried(self):
        self.ad.session = FakeSession([500, 200])
        with self.assertRaises(requests.HTTPError):
            self.ad.get_json("u", {})
        self.assertEqual(self.ad.session.calls, 1)


if __name__ == "__main__":
    unittest.main()
