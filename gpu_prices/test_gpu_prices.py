"""gpu_prices 解析器單元測試 (用迷你頁面片段，不連網)。
執行：cd gpu_prices && ../.venv/bin/python -m unittest test_gpu_prices"""
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect  # noqa: E402
import sources  # noqa: E402


def cell(v):
    return f'<div class="pc-highlight-table-block__cell" style="x">{v}</div>'


NEBIUS = "".join(cell(v) for v in ["Item", "vCPUs", "RAM, GB", "Preemptible, GPU-hour&nbsp;*", "On-demand, GPU-hour",
                                   "NVIDIA GB200 NVL72", "112", "800", "-", "Contact us",
                                   "NVIDIA HGX H200", "16", "200", "from $0.79", "$5.40",
                                   "NVIDIA L40S with Intel CPU", "8–40", "32–160", "from $0.74", "from $1.55"])


def table(head, rows):
    tr = lambda cells, tag: "<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>"
    return "<table>" + tr(head, "th") + "".join(tr(r, "td") for r in rows) + "</table>"


LAMBDA = (table(["Plan", "DURATION", "GPU COUNT", "PRICE/GPU/HR*", "Action"],
                [["NVIDIA HGX B200", "2 weeks – 1 year", "16", "$9.86", "Talk"]])
          + table(["Plan", "VRAM/GPU", "vCPUs", "RAM", "STORAGE", "PRICE/GPU/HR*"],
                  [["NVIDIA H100 SXM", "80 GB", "208", "1800 GiB", "22 TiB SSD", "$3.99"]])
          + table(["Plan", "VRAM/GPU", "vCPUs", "RAM", "STORAGE", "PRICE/GPU/HR*"],
                  [["NVIDIA H100 SXM", "80 GB", "104", "900 GiB", "11 TiB SSD", "$4.09"]]))

RUNPOD_LD = {"@context": "https://schema.org", "@graph": [
    {"@type": "Product", "name": "H200 GPU on Runpod", "offers": {"@type": "AggregateOffer", "offers": [
        {"@type": "Offer", "price": "5.29", "description": "H200 per-hour on-demand rate on Runpod Secure Cloud"},
        {"@type": "Offer", "price": "3.59", "description": "H200 per-hour on-demand rate on Runpod Community Cloud"}]}}]}
RUNPOD = ('<div>141 GB HBM3e</div><div>276 GB RAM</div><div>24</div><div>vCPUs</div><a>Deploy</a><h3>H200</h3>'
          f'<script type="application/ld+json">{json.dumps(RUNPOD_LD)}</script>')


def cw(v):
    return f'<div class="table-v2-cell">{v}</div>'


COREWEAVE = ("".join(cw(v) for v in ["GPU Model", "GPU Count", "VRAM (GB)", "vCPUs", "System RAM (GB)", "Local Storage (TB)",
                                     "On-Demand Price (Per Hour)", "Spot Price (Per Hour)", "Inference Single GPU Price",
                                     "NVIDIA HGX H200", "8", "141", "128", "2,048", "61.44", "$50.44", "$20.93", "$6.31",
                                     "NVIDIA HGX B300", "8", "270", "192", "4,096", "61.44", "Contact sales", "$35.84", ""])
             + "<h2>Europe</h2>"
             + "".join(cw(v) for v in ["GPU Model", "GPU Count", "VRAM (GB)", "vCPUs", "System RAM (GB)", "Local Storage (TB)",
                                       "On-Demand Price (Per Hour)", "Spot Price (Per Hour)",
                                       "NVIDIA HGX H200", "8", "141", "128", "2,048", "61.44", "$50.44", "$20.64"]))


class ParserTest(unittest.TestCase):
    def test_nebius(self):
        rows = sources.parse_nebius(NEBIUS)
        by = {(r["sku"], r["tier"]): r for r in rows}
        self.assertEqual(by[("NVIDIA HGX H200", "on_demand")]["price_gpu_hour_usd"], 5.40)
        self.assertEqual(by[("NVIDIA HGX H200", "spot")]["price_gpu_hour_usd"], 0.79)
        self.assertEqual(by[("NVIDIA HGX H200", "on_demand")]["gpu_model"], "H200")
        self.assertIsNone(by[("NVIDIA GB200 NVL72", "on_demand")]["price_gpu_hour_usd"])
        self.assertNotIn(("NVIDIA GB200 NVL72", "spot"), by)
        l40s = by[("NVIDIA L40S with Intel CPU", "on_demand")]
        self.assertEqual((l40s["vcpu"], l40s["ram_gb"], l40s["price_gpu_hour_usd"]), (8, 32, 1.55))

    def test_lambda(self):
        rows = sources.parse_lambda(LAMBDA)
        od = [r for r in rows if r["tier"] == "on_demand"]
        self.assertEqual([r["gpus_per_instance"] for r in od], [8, 4])
        self.assertEqual(od[0]["price_instance_hour_usd"], 31.92)
        self.assertEqual((od[0]["gpu_model"], od[0]["variant"], od[0]["vram_gb"]), ("H100", "SXM", 80))
        res = [r for r in rows if r["tier"] == "reserved_cluster"][0]
        self.assertEqual((res["gpu_model"], res["price_gpu_hour_usd"], res["gpus_per_instance"]), ("B200", 9.86, 16))

    def test_runpod(self):
        rows = sources.parse_runpod(RUNPOD)
        by = {r["tier"]: r for r in rows}
        self.assertEqual(by["on_demand"]["price_gpu_hour_usd"], 5.29)
        self.assertEqual(by["community"]["price_gpu_hour_usd"], 3.59)
        self.assertEqual((by["on_demand"]["vram_gb"], by["on_demand"]["ram_gb"], by["on_demand"]["vcpu"]), (141, 276, 24))

    def test_coreweave(self):
        rows = sources.parse_coreweave(COREWEAVE)
        na = {(r["tier"], r["sku"]): r for r in rows if r["region"] == "North America"}
        eu = {(r["tier"], r["sku"]): r for r in rows if r["region"] == "Europe"}
        self.assertEqual(na[("on_demand", "NVIDIA HGX H200")]["price_gpu_hour_usd"], 6.31)
        self.assertEqual(na[("on_demand", "NVIDIA HGX H200")]["price_instance_hour_usd"], 50.44)
        self.assertEqual(na[("spot", "NVIDIA HGX H200")]["price_gpu_hour_usd"], 2.62)
        self.assertEqual(na[("inference", "NVIDIA HGX H200")]["price_gpu_hour_usd"], 6.31)
        self.assertIsNone(na[("on_demand", "NVIDIA HGX B300")]["price_gpu_hour_usd"])
        self.assertEqual(eu[("spot", "NVIDIA HGX H200")]["price_gpu_hour_usd"], 2.58)
        self.assertNotIn(("inference", "NVIDIA HGX H200"), eu)

    def test_normalize(self):
        self.assertEqual(sources.normalize("NVIDIA H100 PCIe"), ("H100", "PCIe"))
        self.assertEqual(sources.normalize("A100 SXM 40GB"), ("A100 40GB", "SXM"))
        self.assertEqual(sources.normalize("Pro 6000 MIG 48GB"), ("RTX PRO 6000 MIG 48GB", ""))
        self.assertEqual(sources.normalize("NVIDIA A100", 40), ("A100 40GB", ""))
        self.assertEqual(sources.normalize("RTX 6000 Ada"), ("RTX 6000 Ada", ""))
        self.assertEqual(sources.normalize("NVIDIA GB200 NVL72"), ("GB200 NVL72", ""))


class CollectTest(unittest.TestCase):
    def test_from_dir_save_and_compare(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "src"; src.mkdir()
            for name, page in (("nebius", NEBIUS), ("lambda", LAMBDA), ("runpod", RUNPOD), ("coreweave", COREWEAVE)):
                (src / f"{name}.html").write_text(page, encoding="utf-8")
            limits, sources.MIN_ROWS = sources.MIN_ROWS, {k: 1 for k in sources.MIN_ROWS}
            try:
                df, stats = collect.collect(Path(d), date(2026, 10, 20), from_dir=src)
                self.assertEqual(stats["failed"], [])
                n = collect.save(df, Path(d))
                self.assertEqual(n, len(df))
                collect.save(df, Path(d))                                  # 同一天重跑不會重複
                self.assertEqual(len(__import__("pandas").read_parquet(Path(d) / "prices.parquet")), n)
                piv = collect.compare(df)
                self.assertEqual(piv.loc["H200", "nebius"], 5.40)
                self.assertEqual(piv.loc["H200", "coreweave"], 6.31)
                self.assertEqual(piv.loc["H200", "runpod"], 5.29)
            finally:
                sources.MIN_ROWS = limits


if __name__ == "__main__":
    unittest.main()
