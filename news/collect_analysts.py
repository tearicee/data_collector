#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
產業分析師發文收集 → /mnt/d/mops/news/analyst/分析師發文_YYYY-MM.parquet
================================================================
來源 (SOURCES)：
  - 郭明錤 Medium  https://medium.com/feed/@mingchikuo  —— RSS 含全文 (中、英文各一篇)，只給最近 10 篇
  - 郭明錤 X       syndication.twitter.com 嵌入時間軸 (免登入，但經常 429 限流) → 盡力而為，
                   抓得到就存，抓不到不算失敗。要穩定取得 X 全部貼文需官方 X API 金鑰。
  - SemiAnalysis、Stratechery、TrendForce 集邦 (英文新聞)、Fabricated Knowledge、The Information、
    Mark Gurman (彭博作者 RSS，只有標題)、MacRumors (只留提到名單分析師的文章)
  - 媒體轉述：陸行之、楊應超、蒲得宇、Ross Young、Counterpoint、IDC、Omdia 等沒有公開 RSS
    (Facebook/券商報告/付費牆)，改從新聞庫找提到他們的報導 (MENTIONS)
分析師本人的新發文 (Medium/X) 會推到 Discord，一律經 push_discord.guarded_send 防呆
(只送 6 小時內、未推過、分數達門檻者，且有單次與每小時上限)；媒體轉述不在此推送。
另輸出關鍵字清單供人工整理：/mnt/d/mops/news/mapping/分析師關鍵字_<作者>.csv
cron：每 15 分鐘。
"""
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import download_news as dn  # noqa: E402
from common import heartbeat  # noqa: E402

OUT_DIR = Path("/mnt/d/mops/news/analyst")
MAP_DIR = Path("/mnt/d/mops/news/mapping")
STOCK_INFO = "/mnt/d/finmind_data/TaiwanStockInfo/TaiwanStockInfo.parquet"
FIELDS = ["發布時間", "作者", "平台", "語言", "標題", "內文", "連結", "互動數", "抓取時間"]
NS = {"content": "http://purl.org/rss/1.0/modules/content/"}
SOURCES = [
    {"作者": "郭明錤", "平台": "Medium", "type": "rss", "url": "https://medium.com/feed/@mingchikuo", "push": True},
    {"作者": "郭明錤", "平台": "X", "type": "x", "handle": "mingchikuo", "push": True},
    {"作者": "SemiAnalysis", "平台": "網站", "type": "rss", "url": "https://semianalysis.com/feed/", "push": False},
    {"作者": "Stratechery", "平台": "網站", "type": "rss", "url": "https://stratechery.com/feed/", "push": False},
    {"作者": "TrendForce集邦", "平台": "網站", "type": "rss", "url": "https://www.trendforce.com/news/feed/", "push": False},
    {"作者": "Fabricated Knowledge", "平台": "網站", "type": "rss", "url": "https://www.fabricatedknowledge.com/feed", "push": False},
    {"作者": "The Information", "平台": "網站", "type": "rss", "url": "https://www.theinformation.com/feed", "push": False},
    {"作者": "Mark Gurman", "平台": "彭博", "type": "rss",
     "url": "https://www.bloomberg.com/authors/AS7Hj1mBMGM/mark-gurman.rss", "push": False, "lang": "英文"},
    # MacRumors 只留提到下列分析師的文章 (作者欄記為被引用的人)
    {"作者": "MacRumors", "平台": "媒體轉述", "type": "rss", "url": "https://feeds.macrumors.com/MacRumors-All",
     "push": False, "mention_only": True},
]
# 沒有自己的公開發文管道 (Facebook、券商報告、付費牆) 者：從新聞庫找提到他們的報導，記為「媒體轉述」
MENTIONS = {
    "陸行之": r"陸行之", "楊應超": r"楊應超", "蒲得宇": r"蒲得宇|Jeff Pu", "Ross Young": r"Ross Young|DSCC",
    "Mark Gurman": r"古爾曼|Gurman", "郭明錤": r"郭明錤|Ming-Chi Kuo", "TrendForce集邦": r"集邦|TrendForce",
    "Counterpoint": r"Counterpoint", "IDC": r"(?<![A-Za-z])IDC(?![A-Za-z])", "Omdia": r"Omdia",
}
MENTION_DAYS = 3


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def lang_of(text: str) -> str:
    zh = len(re.findall(r"[一-鿿]", text[:400]))
    return "中文" if zh >= 40 else "英文"


def rss(src: dict) -> list:
    root = ET.fromstring(dn.get(src["url"]).content)
    rows = []
    for it in root.iter("item"):
        body = dn.clean_html(it.findtext("content:encoded", namespaces=NS) or it.findtext("description") or "")
        # Medium 會把首段重複一次當導言
        lines = body.split("\n")
        if len(lines) > 1 and lines[0].strip() == lines[1].strip():
            body = "\n".join(lines[1:])
        title = dn.clean_html(it.findtext("title"))
        author = src["作者"]
        if src.get("mention_only"):
            hit = next((k for k, pat in MENTIONS.items() if re.search(pat, f"{title} {body}")), None)
            if not hit:
                continue
            author = hit
        rows.append({"發布時間": dn.parse_pubdate(it.findtext("pubDate")).strftime("%Y-%m-%d %H:%M:%S"),
                     "作者": author, "平台": src["平台"], "語言": src.get("lang") or lang_of(title + body),
                     "標題": dn.clean_html(it.findtext("title")), "內文": body,
                     "連結": dn.clean_link(it.findtext("link")), "互動數": None, "抓取時間": now()})
    return rows


def x_timeline(src: dict) -> list:
    """X 嵌入時間軸 (最近約 20 則)。429/非 200 直接回空清單。"""
    r = dn.SESSION.get(f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{src['handle']}", timeout=30)
    if r.status_code != 200:
        print(f"{now()} X @{src['handle']}: HTTP {r.status_code} (略過)", flush=True)
        return []
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text, re.S)
    if not m:
        return []
    entries = json.loads(m.group(1))["props"]["pageProps"].get("timeline", {}).get("entries", [])
    rows = []
    for e in entries:
        tw = e.get("content", {}).get("tweet", {})
        text = tw.get("full_text") or tw.get("text") or ""
        if not tw.get("id_str") or not text:
            continue
        t = pd.to_datetime(tw.get("created_at"), errors="coerce", utc=True)
        if pd.isna(t):
            continue
        rows.append({"發布時間": t.tz_convert("Asia/Taipei").strftime("%Y-%m-%d %H:%M:%S"), "作者": src["作者"],
                     "平台": "X", "語言": lang_of(text), "標題": text.split("\n")[0][:120], "內文": text,
                     "連結": f"https://x.com/{src['handle']}/status/{tw['id_str']}",
                     "互動數": float((tw.get("favorite_count") or 0) + (tw.get("retweet_count") or 0)),
                     "抓取時間": now()})
    return rows


def media_mentions() -> list:
    """新聞庫最近 MENTION_DAYS 天內，標題或內文前 300 字提到名單人物/機構的報導。"""
    import store
    since = (pd.Timestamp.now() - pd.Timedelta(days=MENTION_DAYS)).strftime("%Y-%m-%d")
    df = store.read_range(since)
    head = df["標題"].fillna("") + " " + df["內文"].fillna("").str[:300]
    rows = []
    for who, pat in MENTIONS.items():
        m = df[head.str.contains(pat, regex=True, na=False)]
        for r in m.itertuples(index=False):
            rows.append({"發布時間": r.發布時間.strftime("%Y-%m-%d %H:%M:%S"), "作者": who, "平台": f"媒體轉述({r.來源})",
                         "語言": "中文", "標題": r.標題, "內文": r.內文, "連結": f"{r.連結}#{who}",
                         "互動數": None, "抓取時間": now()})
    return rows


def upsert(rows: list) -> list:
    """寫入並回傳新發文 (依連結判斷)。"""
    if not rows:
        return []
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows).reindex(columns=FIELDS)
    df["發布時間"] = pd.to_datetime(df["發布時間"])
    df["抓取時間"] = pd.to_datetime(df["抓取時間"])
    new_all = []
    for ym, g in df.groupby(df["發布時間"].dt.strftime("%Y-%m")):
        path = OUT_DIR / f"分析師發文_{ym}.parquet"
        old = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=FIELDS)
        new = g[~g["連結"].isin(set(old["連結"]))].drop_duplicates("連結")
        if new.empty:
            continue
        out = pd.concat([old, new], ignore_index=True).sort_values("發布時間")
        tmp = path.with_suffix(".parquet.tmp")
        out.to_parquet(tmp, compression="zstd", index=False)
        tmp.replace(path)
        new_all += new.to_dict("records")
    return new_all


# ------------------------------------------------------------------ 關鍵字清單
PERIOD_RE = re.compile(r"^(?:[1-4]?[QH]\d{2}|[1-4][QH]|Q[1-4]|\d{4}|FY\d{2,4}|20\d{2}[EF]?|WWDC\d*|CY\d+)$")
STOP_EN = set("The This That In On At As It If We My A An And Or But For Of To With From By Is Are Was Be I "
              "However Therefore Although While Since After Before Note Based Given Despite Due According "
              "CEO CFO USD YoY QoQ EPS GAAP vs etc Just Both Not No Yes Its Their Our These Those Here There "
              "Medium Twitter Figure Table Source Key Points Conclusion Summary Latest Recent Notably Previous Compared "
              "January February March April May June July August September October November December Updated New "
              "When What Why How Which Who Where Also Even Still Such Some Most Many More Less Only Then Thus So "
              "First Second Third Overall Currently Meanwhile Specifically Previously Instead Rather Because Once "
              "UserId They He She You His Her Any All Each Every Another Other Under Over Into About Between".split())
EN_TERM = re.compile(r"[A-Z][A-Za-z0-9]*(?:[\-/\.][A-Za-z0-9]+)*(?: (?:[A-Z][A-Za-z0-9\-]*|\d{2,4}[A-Za-z]*)){0,2}")
ZH_COMPANY = re.compile(r"[一-鿿]{2,5}(?:科技|電子|精密|材料|半導體|光電|電機|工業|集團|控股)")


def keyword_table(author: str) -> pd.DataFrame:
    posts = pd.concat(pd.read_parquet(f) for f in sorted(OUT_DIR.glob("分析師發文_*.parquet")))
    posts = posts[posts["作者"] == author]
    info = pd.read_parquet(STOCK_INFO).sort_values("date").drop_duplicates("stock_id", keep="last")
    info = info[info["stock_id"].str.fullmatch(r"[1-9]\d{3}") & info["type"].isin(["twse", "tpex"])]
    tw_names = {n: c for n, c in zip(info["stock_name"], info["stock_id"]) if len(n) >= 2}
    from event_rules.rules import ENTITIES, THEMES
    foreign = set()
    for pat in ENTITIES.values():
        foreign |= set(pat.split("|"))
    theme_kw = {}
    for th, pat in THEMES.items():
        for k in pat.split("|"):
            theme_kw[k] = th
    stat = defaultdict(lambda: {"次數": 0, "links": set(), "首見": None, "最近": None, "例句": ""})

    def add(term, link, t, sent):
        s = stat[term]
        s["次數"] += 1
        s["links"].add(link)
        s["首見"] = t if s["首見"] is None or t < s["首見"] else s["首見"]
        s["最近"] = t if s["最近"] is None or t > s["最近"] else s["最近"]
        if not s["例句"] and sent:
            s["例句"] = sent.strip()[:90]

    for p in posts.itertuples(index=False):
        text = re.sub(r"https?://\S+", " ", f"{p.標題}\n{p.內文}")
        for sent in re.split(r"[\n。；;]", text):
            for m in EN_TERM.finditer(sent):
                term = m.group(0).strip(" .-")
                words = term.split(" ")
                while words and (words[0] in STOP_EN or PERIOD_RE.match(words[0])):
                    words = words[1:]
                while words and (words[-1] in STOP_EN or PERIOD_RE.match(words[-1])):
                    words = words[:-1]
                term = " ".join(words)
                if len(term) >= 2 and not PERIOD_RE.match(term) and not term.isdigit():
                    add(term, p.連結, p.發布時間, sent if p.語言 == "中文" else "")
            if p.語言 == "中文":
                for m in ZH_COMPANY.finditer(sent):
                    add(m.group(0), p.連結, p.發布時間, sent)
                for n in tw_names:
                    if n in sent and (len(n) >= 3 or sent.count(n) >= 1 and re.search(rf"{re.escape(n)}[（(]?\d{{4}}", sent)):
                        add(n, p.連結, p.發布時間, sent)
    rows = []
    for term, s in stat.items():
        if term in tw_names:
            kind = "台股公司"
        elif term in foreign or ZH_COMPANY.fullmatch(term):
            kind = "公司"
        elif re.search(r"\d", term) and len(term) <= 14:
            kind = "產品型號/料號"
        elif term.isupper() and len(term) <= 6:
            kind = "技術/材料縮寫"
        else:
            kind = "英文名詞(公司/產品/技術)"
        rows.append({"關鍵字": term, "系統判斷類型": kind, "次數": s["次數"], "文章數": len(s["links"]),
                     "首見": str(s["首見"])[:10], "最近": str(s["最近"])[:10], "台股代號": tw_names.get(term, ""),
                     "對應題材(系統)": next((th for k, th in theme_kw.items() if k.lower() == term.lower()), ""),
                     "例句": s["例句"], "我的分類(公司/產品/技術/材料/忽略)": "", "相關台股(代號|分隔)": "", "歸入題材": ""})
    out = pd.DataFrame(rows).sort_values(["文章數", "次數"], ascending=False)
    path = MAP_DIR / f"分析師關鍵字_{author}.csv"
    if path.exists():  # 保留人工已填欄位
        old = pd.read_csv(path, dtype=str, keep_default_na=False).set_index("關鍵字")
        for c in ("我的分類(公司/產品/技術/材料/忽略)", "相關台股(代號|分隔)", "歸入題材"):
            if c in old:
                out[c] = out["關鍵字"].map(old[c].to_dict()).fillna("")
    out.to_csv(path, index=False, encoding="utf-8-sig")
    return out


ORIGINAL_SCORE = {"Medium": 8.0, "X": 7.0}   # 分析師本人發文視為高分；媒體轉述交給新聞評分流程，不在這裡推


def push(new: list, test: bool = False) -> None:
    """經 push_discord.guarded_send 防呆：只送 6 小時內的新發文、分數達門檻、且有單次/每小時上限。"""
    from event_rules import push_discord as pd_
    items = []
    for r in new:
        if r["平台"] not in ORIGINAL_SCORE:
            continue
        if r["語言"] != "中文" and any(n["作者"] == r["作者"] and n["語言"] == "中文" and
                                      abs((n["發布時間"] - r["發布時間"]).total_seconds()) < 600 for n in new):
            continue  # 同一篇的英文版不重複推
        score = ORIGINAL_SCORE[r["平台"]] if len(r["內文"]) >= 150 else 5.0
        text = (f"{r['發布時間']:%Y-%m-%d %H:%M:%S} <{score:.1f}> － {r['作者']} － <{r['標題'][:90]}> "
                f"<{r['連結'].split('#')[0]}>")
        items.append({"key": r["連結"], "time": r["發布時間"], "score": score, "text": text})
    pd_.guarded_send(items, test=test)


def main() -> int:
    total, errors = 0, []
    first_run = not any(OUT_DIR.glob("分析師發文_*.parquet"))
    for src in SOURCES:
        try:
            rows = rss(src) if src["type"] == "rss" else x_timeline(src)
            new = upsert(rows)
            total += len(new)
            print(f"{now()} {src['作者']}/{src['平台']}: 取得 {len(rows)}，新增 {len(new)}", flush=True)
            if new and src.get("push") and not first_run and "--no-push" not in sys.argv:
                push(new)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{src['作者']}/{src['平台']}")
            print(f"{now()} [ERROR] {src['作者']}/{src['平台']}: {e}", flush=True)
        time.sleep(2)
    try:
        new = upsert(media_mentions())
        total += len(new)
        by = pd.Series([n["作者"] for n in new]).value_counts().to_dict() if new else {}
        print(f"{now()} 媒體轉述: 新增 {len(new)} {by}", flush=True)
    except Exception as e:  # noqa: BLE001
        errors.append("媒體轉述")
        print(f"{now()} [ERROR] 媒體轉述: {e}", flush=True)
    if total or "--keywords" in sys.argv:
        for author in {s["作者"] for s in SOURCES if s.get("push")}:
            k = keyword_table(author)
            print(f"{now()} 關鍵字清單 {author}: {len(k)} 個")
    ok = len(errors) < len(SOURCES)
    heartbeat.write("news_analyst", heartbeat.STATUS_OK if ok else heartbeat.STATUS_FAIL, 0 if ok else 1,
                    f"新增 {total}", stats={"new": total, "failed": errors})
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
