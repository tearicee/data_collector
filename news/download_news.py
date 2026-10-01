#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
財經新聞爬蟲 → Parquet (見 store.py)
================================================================
來源：
  - 鉅亨網 API  api.cnyes.com/media/api/v1/newslist/category/{tw_stock|headline}
      可帶 startAt/endAt (epoch 秒) 回查歷史 (實測 2023 年起可用)，清單即含全文、
      個股代號 (market[].code)、關鍵字。
  - RSS：經濟日報 (產業/股市/國際)、科技新報、工商時報 (證券/產業/科技/政策/金融/兩岸)
      另有自由財經、中央社 (財經/產經)、MoneyDJ。
  - Yahoo 股市 (轉載多家)、DIGITIMES (RSS 全文)、ETtoday 財經、MoneyDJ 台股/產業頻道
  - 中時新聞網財經：RSS/一般請求被擋，用 curl_cffi 模擬瀏覽器抓列表頁 + 原文
  - 聯合新聞網 udn.com (產經/股市；個股情報在這裡，不在 money.udn.com)：RSS 壞掉 (標題空白)，
      改用列表 API udn.com/api/more?type=cate_latest_news&cate_id=6644|6645，每頁 6 則往回翻。
      RSS 只有最近的文章且只給摘要 → 新連結逐篇抓原文頁取完整內文 (BODY_XPATH)；
      抓不到時保留摘要，--mode fulltext 可事後補抓。

模式：
  --mode poll                     抓最近 --hours 小時 (預設 6)，cron 每 15 分鐘
  --mode backfill --start --end   鉅亨逐日回補 (由新到舊)，進度存 state/，可中斷續跑
  --mode fulltext                 對已存但內文過短的 RSS 來源文章補抓完整內文

爬蟲禮儀：單線、每次請求間隔 1.5~3 秒 (回補每 50 次請求長休 20~40 秒)、
429/403/5xx 指數退避、連續 5 次失敗即中止。
"""
import argparse
import html
import json
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
from lxml import html as lxml_html

DC_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DC_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import heartbeat  # noqa: E402
import store  # noqa: E402

CNYES_API = "https://api.cnyes.com/media/api/v1/newslist/category/{cat}"
CNYES_CATS = {"tw_stock": "台股", "headline": "頭條"}
RSS_FEEDS = [
    ("經濟日報", "產業", "https://money.udn.com/rssfeed/news/1001/5591"),
    ("經濟日報", "股市", "https://money.udn.com/rssfeed/news/1001/5590"),
    ("經濟日報", "國際", "https://money.udn.com/rssfeed/news/1001/12017"),
    ("科技新報", "科技", "https://technews.tw/feed/"),
] + [("工商時報", name, f"https://www.ctee.com.tw/rss_web/livenews/{c}") for c, name in [
    ("stock", "證券"), ("industry", "產業"), ("tech", "科技"),
    ("policy", "政策"), ("finance", "金融"), ("china", "兩岸")]] + [
    ("自由財經", "財經", "https://news.ltn.com.tw/rss/business.xml"),
    ("中央社", "財經", "https://feeds.feedburner.com/rsscna/finance"),
    ("中央社", "產經", "https://feeds.feedburner.com/rsscna/technology"),
    ("MoneyDJ", "新聞", "https://www.moneydj.com/kmdj/RssCenter.aspx?svc=NR&fno=1&arg=MB010000"),
    ("MoneyDJ", "台股", "https://www.moneydj.com/kmdj/RssCenter.aspx?svc=NW&fno=1&arg=X0000000"),
    ("MoneyDJ", "產業", "https://www.moneydj.com/kmdj/RssCenter.aspx?svc=NW&fno=1&arg=X1000000"),
    ("Yahoo股市", "新聞", "https://tw.stock.yahoo.com/rss?category=tw-market"),   # 轉載多家媒體 (財訊快報、中央社…)
    ("DIGITIMES", "科技", "https://www.digitimes.com.tw/rss/news.xml"),
    ("ETtoday財經", "財經", "https://feeds.feedburner.com/ettoday/finance"),
]
# 中時新聞網財經：RSS 與一般請求都被擋 (403/404)，用 curl_cffi 模擬瀏覽器抓列表頁 + 原文
CHINATIMES_LIST = "https://www.chinatimes.com/money/?chdtv"
IMPERSONATE = {"中時新聞網"}
UDN_API = "https://udn.com/api/more"
UDN_CATES = {"6644": "產經", "6645": "股市"}
UDN_MAX_PAGES = 25
BODY_XPATH = {  # 原文頁的內文段落
    "聯合新聞網": "//section[contains(@class,'article-content__editor')]//p",
    "經濟日報": "//section[contains(@class,'article-body__editor')]//p",
    "科技新報": "//div[contains(@class,'indent')]//p",
    "工商時報": "//article//p",
    "自由財經": "//div[@class='text']//p[not(@class)]",
    "中央社": "//div[@class='paragraph']//p",
    "MoneyDJ": "//*[@id='highlight']",
    "Yahoo股市": "//article//p[not(contains(.,'Google 偏好來源')) and not(contains(.,'設為首選來源'))]",
    "DIGITIMES": "//div[contains(@class,'content')]//p[not(contains(.,'{'))]",
    "ETtoday財經": "//div[@class='story']//p[not(starts-with(normalize-space(.),'▲'))]",
    "中時新聞網": "//div[contains(@class,'article-body')]//p",
}
MIN_BODY = 200  # 內文短於此視為只有摘要
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "application/json, text/xml, */*",
})
SLEEP = (1.5, 3.0)
PROGRESS = store.STATE_DIR / "cnyes_backfill_done.json"


def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"{ts()} {msg}", flush=True)


def clean_html(text: str) -> str:
    text = html.unescape(html.unescape(text or ""))
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.S)
    text = re.sub(r"</(p|div|br|li|h\d)>|<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t\xa0]+", " ", text)).strip()


def get(url: str, retries: int = 4, **kw) -> requests.Response:
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = SESSION.get(url, timeout=30, **kw)
            if r.status_code in (403, 429):
                wait = int(r.headers.get("Retry-After") or 0) or 60 * attempt
                raise RuntimeError(f"HTTP {r.status_code} 疑似限流，等 {wait}s")
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            wait = locals().get("wait") or min(5 * 2 ** attempt, 120)
            log(f"  [WARN] 第 {attempt}/{retries} 次失敗: {e}；{wait}s 後重試")
            time.sleep(wait)
            wait = None
    raise RuntimeError(f"GET 失敗 {url[:90]}: {last}")


# ------------------------------------------------------------------ 鉅亨
def cnyes_range(cat: str, start_ts: int, end_ts: int, counter: list) -> list:
    rows, page, last_page = [], 1, 1
    while page <= last_page:
        time.sleep(random.uniform(*SLEEP))
        counter[0] += 1
        if counter[0] % 50 == 0:
            time.sleep(random.uniform(20, 40))
        items = get(CNYES_API.format(cat=cat), params={
            "startAt": start_ts, "endAt": end_ts, "limit": 30, "page": page}).json()["items"]
        last_page = items.get("last_page") or 1
        for d in items.get("data") or []:
            codes = [m.get("code", "") for m in (d.get("market") or []) if m.get("code")]
            rows.append({
                "發布時間": datetime.fromtimestamp(int(d["publishAt"])).strftime("%Y-%m-%d %H:%M:%S"),
                "來源": "鉅亨網", "分類": CNYES_CATS[cat],
                "標題": clean_html(d.get("title")), "摘要": clean_html(d.get("summary")),
                "內文": clean_html(d.get("content")),
                "連結": f"https://news.cnyes.com/news/id/{d['newsId']}",
                "個股代號": ",".join(codes), "關鍵字": ",".join(d.get("keyword") or []),
                "抓取時間": ts(),
            })
        page += 1
    return rows


def cnyes_day(d: date, counter: list) -> list:
    s = int(datetime(d.year, d.month, d.day).timestamp())
    rows = []
    for cat in CNYES_CATS:
        rows.extend(cnyes_range(cat, s, s + 86399, counter))
    return rows


# ------------------------------------------------------------------ RSS
def get_html(link: str, source: str = "") -> str:
    if source in IMPERSONATE:
        from curl_cffi import requests as cr
        r = cr.get(link, impersonate="chrome", timeout=30)
        r.raise_for_status()
        return r.text
    return get(link, retries=2).content.decode("utf-8", "ignore")


def chinatimes_rows(since: datetime) -> list:
    """中時財經首頁列表 (h3.title a + time)。"""
    time.sleep(random.uniform(*SLEEP))
    tree = lxml_html.fromstring(get_html(CHINATIMES_LIST, "中時新聞網"))
    rows = []
    for li in tree.xpath("//*[(self::h3 or self::h2) and contains(@class,'title')]/a/ancestor::*[self::li or self::article or self::div][1]"):
        a = li.xpath(".//*[(self::h3 or self::h2) and contains(@class,'title')]/a")
        tm = li.xpath(".//time/@datetime") or ["".join(li.xpath(".//time//text()"))]
        if not a:
            continue
        m = re.search(r"(\d{2}:\d{2}).*?(\d{4}/\d{2}/\d{2})", tm[0]) or re.search(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})", tm[0])
        if not m:
            continue
        pub = (datetime.strptime(f"{m[2]} {m[1]}", "%Y/%m/%d %H:%M") if "/" in m[2]
               else datetime.strptime(f"{m[1]} {m[2]}", "%Y-%m-%d %H:%M"))
        if pub < since:
            continue
        rows.append({"發布時間": pub.strftime("%Y-%m-%d %H:%M:%S"), "來源": "中時新聞網", "分類": "財經",
                     "標題": clean_html(a[0].text_content()), "摘要": "", "內文": "",
                     "連結": clean_link(a[0].get("href")), "個股代號": "", "關鍵字": "", "抓取時間": ts()})
    return rows


def fetch_body(source: str, link: str) -> str:
    """抓原文頁完整內文；失敗回空字串 (呼叫端保留摘要)。"""
    try:
        time.sleep(random.uniform(*SLEEP))
        tree = lxml_html.fromstring(get_html(link, source))
        for bad in tree.xpath("//script|//style|//figure|//figcaption"):
            bad.drop_tree()
        ps = [re.sub(r"\s+", " ", p.text_content()).strip() for p in tree.xpath(BODY_XPATH[source])]
        return "\n".join(p for p in ps if p)
    except Exception as e:  # noqa: BLE001
        log(f"  [WARN] 內文抓取失敗 {link}: {e}")
        return ""


def clean_link(link: str) -> str:
    """去掉 utm 等追蹤參數，保留文章本身的參數 (MoneyDJ 的 a=...)。"""
    link = (link or "").strip()
    base, _, query = link.partition("?")
    keep = [q for q in query.split("&") if q and not re.match(r"(utm_|from=|fbclid)", q)]
    return base + ("?" + "&".join(keep) if keep else "")


def parse_pubdate(s: str) -> datetime:
    s = (s or "").strip()
    try:
        return parsedate_to_datetime(s).astimezone().replace(tzinfo=None)
    except Exception:  # noqa: BLE001  工商時報為 ISO 格式 (台灣時間，無時區)
        dt = datetime.fromisoformat(s)
        return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


def rss_rows(source: str, cat: str, url: str, since: datetime) -> list:
    time.sleep(random.uniform(*SLEEP))
    root = ET.fromstring(get(url).content)
    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    rows = []
    for it in root.iter("item"):
        try:
            pub = parse_pubdate(it.findtext("pubDate"))
        except Exception:  # noqa: BLE001
            continue
        if pub < since:
            continue
        desc = clean_html(it.findtext("description"))
        body = clean_html(it.findtext("content:encoded", namespaces=ns)) or desc
        rows.append({
            "發布時間": pub.strftime("%Y-%m-%d %H:%M:%S"), "來源": source, "分類": cat,
            "標題": clean_html(it.findtext("title")), "摘要": desc[:300], "內文": body,
            "連結": clean_link(it.findtext("link")),
            "個股代號": "", "關鍵字": ",".join(c.text or "" for c in it.findall("category")),
            "抓取時間": ts(),
        })
    return rows


def udn_rows(cate: str, since: datetime) -> list:
    """聯合新聞網列表 API：由新到舊翻頁，翻到早於 since 或連結全部已存為止。"""
    rows = []
    for page in range(1, UDN_MAX_PAGES + 1):
        time.sleep(random.uniform(*SLEEP))
        lists = get(UDN_API, params={"page": page, "channelId": 2, "type": "cate_latest_news",
                                     "cate_id": cate, "totalRecNo": 200},
                    headers={"Referer": f"https://udn.com/news/cate/2/{cate}"}).json().get("lists") or []
        if not lists:
            break
        old = 0
        for it in lists:
            pub = datetime.strptime(it["time"]["date"], "%Y-%m-%d %H:%M")
            if pub < since:
                old += 1
                continue
            rows.append({
                "發布時間": pub.strftime("%Y-%m-%d %H:%M:%S"), "來源": "聯合新聞網", "分類": UDN_CATES[cate],
                "標題": clean_html(it.get("title")), "摘要": clean_html(it.get("paragraph")),
                "內文": clean_html(it.get("paragraph")),
                "連結": "https://udn.com" + clean_link(it["titleLink"]), "個股代號": "", "關鍵字": "",
                "抓取時間": ts(),
            })
        if old == len(lists):
            break
    return rows


def with_fulltext(source: str, rows: list) -> list:
    """濾掉已存連結，其餘逐篇抓完整內文。"""
    have = set().union(*(store.existing_links(ym) for ym in {r["發布時間"][:7] for r in rows})) if rows else set()
    rows = [r for r in rows if r["連結"] not in have]
    for r in rows:
        body = fetch_body(source, r["連結"])
        if len(body) > len(r["內文"]):
            r["內文"] = body
    return rows


# ------------------------------------------------------------------ 模式
def poll(hours: int) -> int:
    now = datetime.now()
    since = now - timedelta(hours=hours)
    stats, errors, counter = {}, [], [0]
    for cat in CNYES_CATS:
        try:
            rows = cnyes_range(cat, int(since.timestamp()), int(now.timestamp()), counter)
            stats[f"鉅亨-{CNYES_CATS[cat]}"] = (len(rows), store.upsert(rows))
        except Exception as e:  # noqa: BLE001
            errors.append(f"鉅亨-{cat}: {e}")
    for source, cat, url in RSS_FEEDS:
        try:
            rows = with_fulltext(source, rss_rows(source, cat, url, since))
            stats[f"{source}-{cat}"] = (len(rows), store.upsert(rows))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{source}-{cat}: {e}")
    try:
        rows = with_fulltext("中時新聞網", chinatimes_rows(since))
        stats["中時新聞網-財經"] = (len(rows), store.upsert(rows))
    except Exception as e:  # noqa: BLE001
        errors.append(f"中時新聞網: {e}")
    for cate, name in UDN_CATES.items():
        try:
            rows = with_fulltext("聯合新聞網", udn_rows(cate, since))
            stats[f"聯合新聞網-{name}"] = (len(rows), store.upsert(rows))
        except Exception as e:  # noqa: BLE001
            errors.append(f"聯合新聞網-{name}: {e}")
    new = sum(v[1] for v in stats.values())
    log("poll " + " ".join(f"{k}={a}/{n}新" for k, (a, n) in stats.items()) + f" 合計新增 {new}")
    for e in errors:
        log(f"  [ERROR] {e}")
    ok = len(errors) < len(CNYES_CATS) + len(RSS_FEEDS) + len(UDN_CATES) + 1  # 全部來源都失敗才算失敗
    heartbeat.write("news_poll", heartbeat.STATUS_OK if ok else heartbeat.STATUS_FAIL,
                    0 if ok else 1, f"新增 {new}",
                    stats={"new": new, "failed": [e.split(":")[0] for e in errors]})
    return 0 if ok else 1


def fulltext() -> int:
    """已存但內文過短的 RSS 來源文章補抓完整內文 (逐月覆寫)。"""
    fixed = 0
    for path in sorted(store.DATA_DIR.glob("新聞_*.parquet")):
        import pandas as pd
        df = pd.read_parquet(path)
        todo = df[df["來源"].isin(BODY_XPATH) & (df["內文"].str.len() < MIN_BODY)]
        if todo.empty:
            continue
        bodies = {link: fetch_body(src, link) for src, link in zip(todo["來源"], todo["連結"])}
        bodies = {k: v for k, v in bodies.items() if len(v) >= MIN_BODY}
        with store.write_lock():
            df = pd.read_parquet(path)  # 鎖內重讀，避免蓋掉輪詢新寫入的列
            m = df["連結"].isin(bodies)
            df.loc[m, "內文"] = df.loc[m, "連結"].map(bodies)
            tmp = path.with_suffix(".parquet.tmp")
            df.to_parquet(tmp, compression="zstd", index=False)
            tmp.replace(path)
        fixed += len(bodies)
        log(f"  {path.name} 補抓 {len(bodies)}/{len(todo)}")
    log(f"fulltext 共補 {fixed} 則")
    return 0


def backfill(start: date, end: date) -> int:
    store.STATE_DIR.mkdir(parents=True, exist_ok=True)
    done = set(json.loads(PROGRESS.read_text()) if PROGRESS.exists() else [])
    days = [end - timedelta(days=i) for i in range((end - start).days + 1)]  # 由新到舊
    todo = [d for d in days if d.isoformat() not in done]
    log(f"==== 鉅亨回補 {start}~{end}：{len(days)} 天，待抓 {len(todo)} 天 ====")
    counter, fails, total = [0], 0, 0
    for i, d in enumerate(todo, 1):
        try:
            rows = cnyes_day(d, counter)
            added = store.upsert(rows)
            fails = 0
        except Exception as e:  # noqa: BLE001
            fails += 1
            log(f"  [ERROR] {d}: {e} (連續失敗 {fails})")
            if fails >= 5:
                log("[FATAL] 連續 5 天失敗，中止 (重跑可續傳)")
                return 1
            continue
        total += added
        done.add(d.isoformat())
        PROGRESS.write_text(json.dumps(sorted(done)))
        log(f"  [{i}/{len(todo)}] {d} 取得 {len(rows)} 新增 {added}")
    log(f"==== 完成，新增 {total} 則 ====")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="財經新聞爬蟲")
    ap.add_argument("--mode", choices=["poll", "backfill", "fulltext"], default="poll")
    ap.add_argument("--hours", type=int, default=6)
    ap.add_argument("--start")
    ap.add_argument("--end")
    a = ap.parse_args()
    if a.mode == "poll":
        return poll(a.hours)
    if a.mode == "fulltext":
        return fulltext()
    end = date.fromisoformat(a.end) if a.end else date.today() - timedelta(days=1)
    return backfill(date.fromisoformat(a.start), end)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        log(f"[FATAL] {e}")
        sys.exit(1)
