"""alert_log 單元測試。執行：cd common && ../.venv/bin/python -m unittest test_alert_log"""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import alert_log as m


class CompactTailTest(unittest.TestCase):
    def test_drops_noise_and_keeps_last_lines(self):
        text = "\n".join([f"[x] 進度 {i}/100" for i in range(20)] + ["", "等待 6.2 秒...",
                         "元大/a.csv: Copied (new)"] + [f"line {i}" for i in range(15)] + ["Traceback", "Error: boom"])
        out = m.compact_tail(text)
        self.assertEqual(out.count("\n") + 1, m.TAIL_LINES)
        self.assertNotIn("進度", out)
        self.assertNotIn("Copied", out)
        self.assertTrue(out.endswith("Traceback\nError: boom"))

    def test_truncates_to_char_limit_from_the_end(self):
        out = m.compact_tail("a" * 2000 + "\nEND", chars=100)
        self.assertTrue(out.startswith("…") and out.endswith("END"))
        self.assertLessEqual(len(out), 101)


class RecordReadTest(unittest.TestCase):
    def test_round_trip_and_since_filter(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "alerts.jsonl"
            m.record("data_collector", "job_a", "任務執行失敗 (exit=1)", 1, "x\n進度 1/2\ny", sent=True, path=p)
            rows = m.read(path=p)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["job"], "job_a")
            self.assertEqual(rows[0]["tail"], "x\ny")
            self.assertEqual(m.read(datetime.now() + timedelta(minutes=1), path=p), [])
            self.assertIn("job_a (exit=1)", m.summary_lines(rows)[0])

    def test_missing_file_reads_empty(self):
        self.assertEqual(m.read(path=Path("/nonexistent/alerts.jsonl")), [])


if __name__ == "__main__":
    unittest.main()
