#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新聞熱度快照 → /mnt/d/mops/news/heat/熱度快照_YYYY-MM.parquet
  每次執行抓一次「當下」的熱度，累積成時間序列 (之後可用 發布時間 × 點閱成長 建模)：
  - udn_pv    聯合新聞網 產經/股市 熱門排行 udn.com/rank/pv/2/{6644|6645}[/頁]，頁面上有實際點閱數。
              文章編號與經濟日報 money.udn.com 相同，可對回兩站的文章。
              (不可呼叫文章頁裡的 misc.udn.com/record/pageview：那是「記錄」端點，呼叫會灌水)
  - cnyes_pv  鉅亨熱門新聞 API「all」榜：帶實際 pageview
  - cnyes_pop 鉅亨各分類熱門榜 (只有名次)
  - ptt_stock PTT Stock 板文章推文數 (標題含 [新聞] 者為新聞轉貼)
欄位：snapshot、source、key (udn 文章編號 / 鉅亨 newsId / PTT 網址)、title、value (點閱數/名次/推文數)、
      rank、pub_time
cron：每 30 分鐘。
"""
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
from lxml import html as lxml_html

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import download_news as dn  # noqa: E402
from common import heartbeat  # noqa: E402

HEAT_DIR = Path("/mnt/d/mops/news/heat")
UDN_CATES = {"6644": "產經", "6645": "股市"}
UDN_PAGES = 4
PTT_PAGES = 3


def udn_pv(now: str) -> list:
    rows = []
    for cate in UDN_CATES:
        for page in range(1, UDN_PAGES + 1):
            time.sleep(random.uniform(*dn.SLEEP))
            url = f"https://udn.com/rank/pv/2/{cate}" + (f"/{page}" if page > 1 else "")
            tree = lxml_html.fromstring(dn.get(url).content.decode("utf-8", "ignore"))
            items = tree.xpath("//div[contains(@class,'story-list__text')]")
            for i, it in enumerate(items, 1):
                a = it.xpath(".//h2/a|.//h3/a")
                v = [x.strip() for x in it.xpath(".//*[contains(@class,'view')]//text()") if x.strip()]
                m = re.search(r"/(\d{6,})", a[0].get("href", "")) if a else None
                if not (m and v and v[0].replace(",", "").isdigit()):
                    continue
                tm = it.xpath(".//time//text()")
                rows.append({"snapshot": now, "source": "udn_pv", "key": m[1], "title": a[0].text_content().strip(),
                             "value": float(v[0].replace(",", "")), "rank": (page - 1) * 28 + i,
                             "pub_time": tm[0].strip() if tm else ""})
            if len(items) < 20:
                break
    return rows


def cnyes_pop(now: str) -> list:
    """鉅亨熱門：items 依分類分組；「all」那組帶實際 pageview，其餘只有名次。"""
    time.sleep(random.uniform(*dn.SLEEP))
    items = dn.get("https://api.cnyes.com/media/api/v1/newslist/popular", params={"limit": 30}).json().get("items") or {}
    rows, seen = [], set()
    for cat in ["all", "tw_stock", "wd_stock", "tw_money", "future", "forex", "cn_stock"]:
        for i, d in enumerate(items.get(cat) or [], 1):
            if not isinstance(d, dict) or "newsId" not in d:
                continue
            has_pv = d.get("pageview") is not None
            src = "cnyes_pv" if has_pv else "cnyes_pop"
            if (src, d["newsId"]) in seen:
                continue
            seen.add((src, d["newsId"]))
            pub = datetime.fromtimestamp(int(d["publishAt"])).strftime("%Y-%m-%d %H:%M") if d.get("publishAt") else ""
            rows.append({"snapshot": now, "source": src, "key": str(d["newsId"]),
                         "title": dn.clean_html(d.get("title")), "value": float(d["pageview"]) if has_pv else float(i),
                         "rank": i, "pub_time": pub})
    return rows


def ptt_stock(now: str) -> list:
    rows, url = [], "https://www.ptt.cc/bbs/Stock/index.html"
    for _ in range(PTT_PAGES):
        time.sleep(random.uniform(*dn.SLEEP))
        tree = lxml_html.fromstring(dn.get(url, cookies={"over18": "1"}).content.decode("utf-8", "ignore"))
        for ent in tree.xpath("//div[@class='r-ent']"):
            a = ent.xpath(".//div[@class='title']/a")
            if not a:
                continue
            n = "".join(ent.xpath(".//div[@class='nrec']//text()")).strip()
            val = 100.0 if n == "爆" else (-10.0 * int(n[1:]) if n.startswith("X") and n[1:].isdigit()
                                          else float(n) if n.isdigit() else 0.0)
            rows.append({"snapshot": now, "source": "ptt_stock", "key": "https://www.ptt.cc" + a[0].get("href"),
                         "title": a[0].text_content().strip(), "value": val, "rank": 0,
                         "pub_time": "".join(ent.xpath(".//div[@class='date']//text()")).strip()})
        prev = tree.xpath("//a[contains(text(),'上頁')]/@href")
        if not prev:
            break
        url = "https://www.ptt.cc" + prev[0]
    return rows


def main() -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows, errors = [], []
    for name, fn in (("udn_pv", udn_pv), ("cnyes_pop", cnyes_pop), ("ptt_stock", ptt_stock)):
        try:
            got = fn(now)
            rows += got
            print(f"{now} {name}: {len(got)}", flush=True)
        except Exception as e:  # noqa: BLE001
            errors.append(name)
            print(f"{now} [ERROR] {name}: {e}", flush=True)
    if rows:
        HEAT_DIR.mkdir(parents=True, exist_ok=True)
        path = HEAT_DIR / f"熱度快照_{now[:7]}.parquet"
        df = pd.DataFrame(rows)
        df["snapshot"] = pd.to_datetime(df["snapshot"])
        if path.exists():
            df = pd.concat([pd.read_parquet(path), df], ignore_index=True)
        tmp = path.with_suffix(".parquet.tmp")
        df.to_parquet(tmp, compression="zstd", index=False)
        tmp.replace(path)
    ok = len(errors) < 3
    heartbeat.write("news_heat", heartbeat.STATUS_OK if ok else heartbeat.STATUS_FAIL, 0 if ok else 1,
                    f"{len(rows)} 筆", stats={"rows": len(rows), "failed": errors})
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
