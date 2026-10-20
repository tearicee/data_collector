"""四家 GPU 雲的官網價格頁解析器（Nebius、Lambda、RunPod、CoreWeave）。

每家回傳同一種長表列 (dict)，欄位見 COLUMNS。價格一律換成「每 GPU-小時美元」；
頁面只給整台價的 (CoreWeave) 另存整台價並除以 GPU 數。各家頁面都是靜態 HTML 就含價格，
用 requests + 正規表示式解析即可，不開瀏覽器。

tier 的意思：
  on_demand        隨選、不中斷（RunPod 的 Secure Cloud 歸在這裡）
  community        RunPod Community Cloud（第三方機房、較便宜）
  spot             搶占式／Spot；Nebius 給的是動態市價的「地板價」(note 註明)
  reserved_cluster 預留叢集的公告價 (Lambda 1-Click Clusters)
  cluster          RunPod Instant Clusters 公告價
  serverless       RunPod Serverless 每 GPU 工作者小時價
  inference        CoreWeave 推論平台單 GPU 價
"""
from __future__ import annotations

import html
import json
import re
from decimal import ROUND_HALF_UP, Decimal

import requests

COLUMNS = ["provider", "gpu_model", "variant", "sku", "tier", "region", "gpus_per_instance", "vram_gb", "vcpu",
           "ram_gb", "storage", "price_gpu_hour_usd", "price_instance_hour_usd", "commitment", "note", "source_url"]

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0"}
TIMEOUT = 40

URLS = {
    "nebius": "https://nebius.com/prices",
    "lambda": "https://lambda.ai/pricing",
    "runpod": "https://www.runpod.io/pricing",
    "coreweave": "https://www.coreweave.com/pricing",
}

# 各家最少應解析出幾列；少於這個數視為頁面改版，收集失敗要告警
MIN_ROWS = {"nebius": 8, "lambda": 20, "runpod": 40, "coreweave": 20}


def fetch(provider: str) -> str:
    r = requests.get(URLS[provider], headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


# --------------------------------------------------------------------------- #
# 共用小工具
def _text(fragment: str) -> str:
    """去標籤、解 entity、壓空白。"""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _money(text: str | None) -> float | None:
    m = re.search(r"\$\s*([\d,]+\.?\d*)", text or "")
    return float(m.group(1).replace(",", "")) if m else None


def _num(text: str | None) -> float | None:
    """抓第一個數字；'8–40'、'32-160' 這種範圍取最小值。"""
    m = re.search(r"([\d,]+\.?\d*)", text or "")
    return float(m.group(1).replace(",", "")) if m else None


_MODEL_RULES = [
    # (正規式, 標準型號, 變體)
    (r"GB300", "GB300 NVL72", ""), (r"GB200", "GB200 NVL72", ""),
    (r"B300", "B300", ""), (r"B200", "B200", ""), (r"GH200", "GH200", ""),
    (r"H200", "H200", ""),
    (r"H100 ?NVL", "H100", "NVL"), (r"H100 ?PCIe", "H100", "PCIe"), (r"H100", "H100", "SXM"),
    # A100 的 40/80GB 在 normalize 裡依名稱／VRAM 補；變體看 PCIe/SXM
    (r"A100.*?PCIe", "A100", "PCIe"), (r"A100.*?SXM", "A100", "SXM"), (r"A100", "A100", ""),
    # MIG 切片是不同商品，不跟整卡比
    (r"PRO 6000.*?MIG.*?48", "RTX PRO 6000 MIG 48GB", ""), (r"PRO 6000.*?MIG.*?24", "RTX PRO 6000 MIG 24GB", ""),
    (r"RTX PRO 6000|RTX Pro 6000|RTX 6000 Pro", "RTX PRO 6000", ""),
    (r"RTX 6000 Ada|6000 Ada", "RTX 6000 Ada", ""), (r"Quadro RTX 6000", "Quadro RTX 6000", ""),
    (r"L40S", "L40S", ""), (r"L40", "L40", ""), (r"L4\b", "L4", ""),
    (r"A6000", "RTX A6000", ""), (r"A5000", "RTX A5000", ""), (r"A40\b", "A40", ""), (r"A10\b", "A10", ""),
    (r"5090", "RTX 5090", ""), (r"4090", "RTX 4090", ""), (r"3090", "RTX 3090", ""),
    (r"V100", "V100", ""),
]


def normalize(sku: str, vram_gb: float | None = None) -> tuple[str, str]:
    """把各家的商品名對到共同型號；回傳 (標準型號, 變體)。對不到就原樣回傳。
    A100 以名稱裡的 40/80 或 VRAM 分成 'A100 40GB'／'A100 80GB'（都沒有就當 80GB）。"""
    for pat, model, variant in _MODEL_RULES:
        if re.search(pat, sku, re.I):
            if model == "A100":
                mem = 40 if (vram_gb == 40 or (vram_gb is None and re.search(r"\b40", sku))) else 80
                model = f"A100 {mem}GB"
            return model, variant
    return sku, ""


def _cents(x: float | None) -> float | None:
    """四捨五入到分（50.44/8 = 6.305 → 6.31，不用浮點的銀行家捨入）。"""
    if x is None:
        return None
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _row(provider: str, sku: str, tier: str, **kw) -> dict:
    model, variant = normalize(sku, kw.get("vram_gb"))
    row = {c: None for c in COLUMNS}
    row.update(provider=provider, gpu_model=model, variant=kw.pop("variant", variant), sku=sku, tier=tier,
               source_url=URLS[provider], region=kw.pop("region", ""), commitment=kw.pop("commitment", ""),
               note=kw.pop("note", ""))
    row.update(kw)
    return row


# --------------------------------------------------------------------------- #
def parse_nebius(page: str) -> list[dict]:
    """nebius.com/prices：pc-highlight-table 一列 5 格 = 商品 | vCPU | RAM | 搶占價 | 隨選價。
    價格已含 vCPU/RAM (2025-10 起合併計費)；L40S 例外，是從最小組合算起的「from」價。"""
    cells = [_text(c) for c in re.findall(r'<div class="pc-highlight-table-block__cell"[^>]*>(.*?)</div>', page, re.S)]
    rows = []
    for i in range(0, len(cells) - 4):
        sku = cells[i]
        if not sku.startswith("NVIDIA") or cells[i + 1] == "vCPUs":
            continue
        vcpu, ram, spot, od = cells[i + 1:i + 5]
        if not (re.search(r"\d", vcpu) and re.search(r"\d", ram)):
            continue
        base = dict(gpus_per_instance=1, vcpu=_num(vcpu), ram_gb=_num(ram), region="",
                    note=("vCPU/RAM 為範圍，價格為最小組合起價；" if "–" in vcpu or "-" in vcpu else "") + "價格含 vCPU 與 RAM")
        od_price = _money(od)
        rows.append(_row("nebius", sku, "on_demand", price_gpu_hour_usd=od_price,
                         **{**base, "note": base["note"] + ("" if od_price is not None else "；官網僅標 Contact us")}))
        sp = _money(spot)
        if sp is not None:
            note = "動態市價的地板價 (from)，上限為隨選價減 1 美分" if "from" in spot.lower() else "固定搶占價"
            if "L40S" in sku:
                note = "L40S 不走動態市價，固定搶占價 (from = 最小組合)"
            rows.append(_row("nebius", sku, "spot", price_gpu_hour_usd=sp, **{**base, "note": note}))
    return rows


def _html_tables(page: str) -> list[list[list[str]]]:
    tables = []
    for t in re.findall(r"<table.*?</table>", page, re.S):
        rows = []
        for tr in re.findall(r"<tr.*?</tr>", t, re.S):
            cells = [_text(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S)]
            if any(cells):
                rows.append(cells)
        tables.append(rows)
    return tables


def parse_lambda(page: str) -> list[dict]:
    """lambda.ai/pricing：表頭含 VRAM/GPU 的是隨選表，頁面上的分頁依序是 8x、4x、2x、1x；
    表頭含 DURATION 的是 1-Click Clusters 預留價。價格為每 GPU-小時、未含稅。"""
    rows, od_tables = [], []
    for table in _html_tables(page):
        head = [h.upper() for h in table[0]]
        if any("VRAM" in h for h in head):
            od_tables.append(table)
        elif any("DURATION" in h for h in head):
            for r in table[1:]:
                if len(r) < 4 or _money(r[3]) is None:
                    continue
                rows.append(_row("lambda", r[0], "reserved_cluster", gpus_per_instance=_num(r[2]),
                                 price_gpu_hour_usd=_money(r[3]), commitment=r[1],
                                 note=f"1-Click Cluster，{r[2]} 顆 GPU 起、期間 {r[1]}"))
    counts = [8, 4, 2, 1][:len(od_tables)]
    for n, table in zip(counts, od_tables):
        for r in table[1:]:
            if len(r) < 6 or _money(r[5]) is None:
                continue
            price = _money(r[5])
            rows.append(_row("lambda", r[0], "on_demand", gpus_per_instance=n, vram_gb=_num(r[1]), vcpu=_num(r[2]),
                             ram_gb=_num(r[3]), storage=r[4], price_gpu_hour_usd=price,
                             price_instance_hour_usd=round(price * n, 2), note="vCPU/RAM/儲存為整台規格；價格未含稅"))
    return rows


def parse_runpod(page: str) -> list[dict]:
    """runpod.io/pricing：JSON-LD 的 Product 清單給每款 GPU 的 Secure Cloud／Community Cloud 小時價；
    VRAM／RAM／vCPU 從可見卡片補。Serverless 與叢集價另在頁面文字，這裡只取 JSON-LD 有的。"""
    rows = []
    products = []
    for sc in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S):
        try:
            graph = json.loads(sc).get("@graph", [])
        except ValueError:
            continue
        products += [p for p in graph if p.get("@type") == "Product"]
    # 可見卡片的文字：「288 GB HBM3e 251 GB RAM 32 vCPUs Deploy B300」→ 用 JSON-LD 的商品名去對
    text = _text(re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.S))
    specs = {}
    for prod in products:
        sku = prod["name"].replace(" GPU on Runpod", "").strip()
        m = re.search(r"(\d+)\s*GB\s+\w+\s+(\d+)\s*GB RAM\s+(\d+)\s*vCPUs\s+Deploy\s+(?:RTX\s+)?" + re.escape(sku) + r"\b",
                      text, re.I)
        if m:
            specs[sku] = (float(m.group(1)), float(m.group(2)), float(m.group(3)))
    for prod in products:
            sku = prod["name"].replace(" GPU on Runpod", "").strip()
            offers = prod.get("offers", {}).get("offers", [])
            vram, ram, vcpu = specs.get(sku, (None, None, None))
            for off in offers:
                desc = off.get("description", "")
                tier = "on_demand" if "Secure" in desc else "community" if "Community" in desc else "other"
                rows.append(_row("runpod", sku, tier, gpus_per_instance=1, vram_gb=vram, ram_gb=ram, vcpu=vcpu,
                                 price_gpu_hour_usd=float(off["price"]),
                                 note="Secure Cloud (RunPod 自營機房)" if tier == "on_demand" else
                                      "Community Cloud (第三方機房，較便宜、保證較少)" if tier == "community" else desc))
    return rows


def parse_coreweave(page: str) -> list[dict]:
    """coreweave.com/pricing：table-v2-cell 格子序列，表頭後每列 9 格 (北美，含推論價) 或 8 格 (歐洲)：
    型號 | GPU 數 | VRAM | vCPU | RAM | 儲存 | 隨選整台價 | Spot 整台價 | (推論單 GPU 價)。
    Europe 標題之後的表視為歐洲區。"""
    rows = []
    cells = [_text(c) for c in re.findall(r'<div class="table-v2-cell[^"]*">(.*?)</div>', page, re.S)]
    # 頁面依序是北美表、歐洲表 (之後還有手機版卡片，沒有 GPU Model 表頭，不會被當成表)
    regions = ["North America", "Europe"]
    starts = [i for i, c in enumerate(cells) if c == "GPU Model"]
    for block, start in enumerate(starts):
        region = regions[block] if block < len(regions) else f"region{block + 1}"
        header = []
        i = start
        while i < len(cells) and not cells[i].startswith("NVIDIA"):
            header.append(cells[i])
            i += 1
        width = len(header)
        while i + width <= len(cells) and cells[i].startswith("NVIDIA"):
            r = cells[i:i + width]
            i += width
            n = _num(r[1]) or 1
            od, spot = _money(r[6]), _money(r[7])
            base = dict(region=region, gpus_per_instance=n, vram_gb=_num(r[2]), vcpu=_num(r[3]), ram_gb=_num(r[4]),
                        storage=f"{r[5]} TB", note="頁面標整台價，每 GPU 價＝整台價÷GPU 數")
            rows.append(_row("coreweave", r[0], "on_demand", price_instance_hour_usd=od,
                             price_gpu_hour_usd=_cents(od / n) if od else None,
                             **{**base, "note": base["note"] + ("" if od else "；官網僅標 Contact sales")}))
            if spot:
                rows.append(_row("coreweave", r[0], "spot", price_instance_hour_usd=spot,
                                 price_gpu_hour_usd=_cents(spot / n), **base))
            if width >= 9 and _money(r[8]):
                rows.append(_row("coreweave", r[0], "inference", price_gpu_hour_usd=_money(r[8]),
                                 **{**base, "note": "推論平台客戶專用的單 GPU 價"}))
    return rows


PARSERS = {"nebius": parse_nebius, "lambda": parse_lambda, "runpod": parse_runpod, "coreweave": parse_coreweave}
