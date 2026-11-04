#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
事件日曆 (第一層) + 市場預期資料 (第二層)
================================================================
使用者 2026-11-04：每週六晚間推一張「未來一周重大事件 + 市場預期」表；CPI/ADP/PMI 這類高頻數據先不推 (仍存檔)。

來源 (都是本機資料，不上網)：
  1. 重訊：法人說明會 (召開日期)、股東臨時會/股東常會 (開會日期)、新股/增資股上市櫃買賣日、公開收購最後日、減資換發 → 公司事件
  2. 新聞 (近 10 天)：句子裡同時有「未來一周的日期」與事件詞 (財報/法說/央行/選舉/關稅/結算/指數調整/展會/判決…) → 國際與政策事件；
     週日工商「本周國內外財經焦點」、經濟日報「五件財經大事」這類彙整文平常被評分剔除，在這裡是最好的日曆來源。
  3. 預期：同一視窗的新聞句子裡帶 預期/預估/共識/可望/看好/料/目標 的句子，對到事件或個股 → 存成資料 (第二層)，
     之後第三層拿實際結果對它。

輸出：D:/mops/news/calendar/事件日曆_YYYY-MM-DD.parquet (起算日)、預期_YYYY-MM.parquet (累積，鍵 = 來源連結+句子前 40 字)
      Discord 一則 (經 push_discord.guarded_send，key = calendar-<起算日>)
用法：python -m event_rules.event_calendar                 # 週六 21:00 cron：下週一到下週日
      python -m event_rules.event_calendar --start 2026-11-09 --asof 2026-11-07 --dry   # 回測/試跑：假裝在 asof 當天跑
"""
import argparse
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from news import tag_news as TN  # noqa: E402  (個股名稱/別名比對)

NEWS = "/mnt/d/mops/news/data/新聞_{ym}.parquet"
MOPS = "/mnt/d/mops/material_info/data/重大訊息_{ym}.parquet"
SCORED = "/mnt/d/mops/news/scored/新鮮度_{ym}.parquet"
OUT = Path("/mnt/d/mops/news/calendar")
LOOKBACK_DAYS = 10                                  # 往前看幾天的新聞找「下周」的事件
EVENT_RE = re.compile(r"財報|法說|說明會|利率決策|利率會議|央行|聯準會|Fed|FOMC|歐洲央行|日銀|日本央行|理監事會|選舉|公投|罷免|關稅|談判|協議|峰會|會談|川習|"
                      r"結算|掛牌|上市|IPO|除息|除權|開幕|展會|博覽|發表會|發布會|GTC|Computex|CES|判決|裁決|聽證|期限|截止|生效|股東會|年會|論壇|"
                      r"指數.{0,6}(調整|審核|納入|剔除)|MSCI|FTSE|富時|成分股|投票|表決|三讀|審查|開標|決標|停工|復工|罷工|最後交易日")
HIGH_FREQ_RE = re.compile(r"CPI|PPI|PCE|PMI|NMI|ADP|非農|就業報告|失業率|零售銷售|貿易收支|貿易帳|外匯存底|GDP|消費者信心|JOLTS|初領|耐久財|工業生產|景氣燈號|"
                          r"外資淨匯|進出口|出口|外銷訂單|通膨數據|物價")
EXPECT_RE = re.compile(r"預期|預估|預料|預計|市場估|共識|可望|有望|看好|看淡|看多|看空|目標價|估計|料將|恐將|恐|將成|焦點|關注|牽動|考驗|觀察")
NUM_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?\s*(?:%|％|億|萬|元|點|美元|美分|bp|基點|倍)")
DATE_RE = re.compile(r"(?<![\d/])(\d{1,2})月(\d{1,2})日|(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])|(下{1,2}|本)[周週]([一二三四五六日天])")
WEEK = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
WD = "一二三四五六日"
ROUNDUP_RE = re.compile(r"本周|本週|下周|下週|一周|一週|財經焦點|五件|大事|行事曆|不可不知|搶先看|前瞻|展望")
NOISE_RE = re.compile(r"圖擷取|綜合報導|記者|發票|兌領|周年|週年|紀念|講座|研討|募集|申購|收益分配|配息|受益權|抽籤|領息|家屬|法定期限|應該|搞不好|"
                      r"開賣|促銷|優惠|演唱|展演|路跑|比賽|開獎|課程|校園|徵才|招募|公益|捐|刮刮卡|兌換|"
                      r"[參参]加.{0,14}舉辦|受邀|處置股|撮合|處置期間|股款繳納憑證|劃撥|集保|繳納|開募|募集|其他應敘明|報名|"
                      r"公告本公司|公開資訊觀測站|符合條款|訂.{0,8}召開.{0,8}(線上)?(法人說明|法說|業績說明)會|將於.{0,16}舉行線上法人說明會")   # 重訊轉載由 mops_events 負責
STRONG_EXPECT_RE = re.compile(r"預期|預估|預料|市場估|共識|可望|看好|看淡|看多|看空|目標價|估計|料將|恐將|將左右|牽動|考驗")


def _months(start: pd.Timestamp, end: pd.Timestamp) -> list:
    return sorted({d.strftime("%Y-%m") for d in pd.date_range(start.normalize(), end.normalize(), freq="D")})


def _read(pattern: str, start: pd.Timestamp, end: pd.Timestamp, tcol: str) -> pd.DataFrame:
    frames = []
    for ym in _months(start, end):
        p = Path(pattern.format(ym=ym))
        if p.exists():
            frames.append(pd.read_parquet(p))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames)
    df[tcol] = pd.to_datetime(df[tcol])
    return df[(df[tcol] >= start) & (df[tcol] < end)]


def _dates_in(text: str, pub: pd.Timestamp, lo: pd.Timestamp, hi: pd.Timestamp) -> list:
    """句子裡落在 [lo, hi] 的日期。M月D日 / M/D 取 pub 之後最近的一年；下周X 以 pub 所在週推算。"""
    out = []
    for m in DATE_RE.finditer(text):
        if re.search(r"(20\d\d|去|前|當|同|去年同)年$|20\d\d$", text[max(0, m.start() - 6):m.start()]):
            continue                                           # 2024年11月14日：過去的日期
        if m.group(1):
            mo, d = int(m.group(1)), int(m.group(2))
        elif m.group(3):
            mo, d = int(m.group(3)), int(m.group(4))
        else:
            base = pub.normalize() - pd.Timedelta(days=pub.weekday())      # pub 所在週的週一
            weeks = {"本": 0, "下": 1, "下下": 2}[m.group(5)]
            dt = base + pd.Timedelta(days=7 * weeks + WEEK[m.group(6)])
            if lo <= dt <= hi:
                out.append(dt)
            continue
        for y in (pub.year, pub.year + 1):
            try:
                dt = pd.Timestamp(year=y, month=mo, day=d)
            except ValueError:
                continue
            if dt >= pub.normalize() - pd.Timedelta(days=3) and lo <= dt <= hi:
                out.append(dt)
                break
    return sorted(set(out))


def news_events(asof: pd.Timestamp, lo: pd.Timestamp, hi: pd.Timestamp) -> pd.DataFrame:
    news = _read(NEWS, asof - pd.Timedelta(days=LOOKBACK_DAYS), asof, "發布時間")
    if news.empty:
        return pd.DataFrame()
    news = news.drop_duplicates("標題")
    rows = []
    for r in news.itertuples(index=False):
        text = f"{r.標題}\n{r.內文 or ''}"
        roundup = bool(ROUNDUP_RE.search(r.標題))
        for s in re.split(r"[。；\n▪●■]", text):
            s = s.strip()
            if not (12 <= len(s) <= 220) or not EVENT_RE.search(s) or NOISE_RE.search(s):
                continue
            dts = _dates_in(s, r.發布時間, lo, hi)
            if not dts:
                continue
            hf = bool(HIGH_FREQ_RE.search(s)) and not re.search(r"財報|法說|央行|利率|選舉|關稅|結算", s)
            stocks = TN.codes_from_names(s, "")
            for dt in dts:
                rows.append({"日期": dt, "事件": s[:200], "類型": "高頻數據" if hf else "國際/政策/產業", "個股": ",".join(stocks),
                             "來源": r.來源, "發布時間": r.發布時間, "連結": r.連結, "彙整文": roundup,
                             "預期": bool(STRONG_EXPECT_RE.search(s)) and bool(NUM_RE.search(s) or re.search(r"財報|法說|央行|利率|選舉|關稅|結算|指數", s)),
                             "數字": ";".join(NUM_RE.findall(s)[:4])})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["鍵"] = df["事件"].str.replace(r"[\s，,、：:（）()「」]", "", regex=True).str[:22]
    df = df.sort_values(["彙整文", "發布時間"], ascending=[False, False]).drop_duplicates(["日期", "鍵"])
    df["有個股"] = df["個股"] != ""
    return df.sort_values(["日期", "彙整文", "有個股", "發布時間"], ascending=[True, False, False, False]).reset_index(drop=True)


def mops_events(asof: pd.Timestamp, lo: pd.Timestamp, hi: pd.Timestamp, heat: dict, names: dict) -> pd.DataFrame:
    mi = _read(MOPS, asof - pd.Timedelta(days=45), asof, "發布時間")
    if mi.empty:
        return pd.DataFrame()
    rows = []
    kinds = [("法說會", r"法人說明會|法說", r"法人說明會之?日期[:：]\s*(\d{2,3})/(\d{1,2})/(\d{1,2})", r"受邀|取消|延期"),
             ("股東臨時會", r"股東臨時會", r"股東(臨時)?會(召開)?日期[:：]\s*(\d{2,3})/(\d{1,2})/(\d{1,2})", r"取消|延期|決議事項|重要決議"),
             ("股東常會", r"股東常會", r"股東(常|常年)?會(召開)?日期[:：]\s*(\d{2,3})/(\d{1,2})/(\d{1,2})", r"取消|延期|決議事項|重要決議"),
             ("上市櫃買賣日", r"上[市櫃]買賣日|掛牌", r"(?:上[市櫃]買賣|掛牌)日(?:期)?[:：]?\s*(?:民國)?(\d{2,3})/(\d{1,2})/(\d{1,2})", r"取消"),
             ("公開收購截止", r"公開收購", r"(?:公開收購期間|截止|最後).{0,30}?(\d{2,3})/(\d{1,2})/(\d{1,2})", r"取消"),
             ("減資換發", r"減資.{0,8}換發|換發新股", r"(?:換發|新股上[市櫃]).{0,20}?(\d{2,3})/(\d{1,2})/(\d{1,2})", r"取消")]
    for kind, subj, date_rx, excl in kinds:
        g = mi[mi["主旨"].str.contains(subj, na=False) & ~mi["主旨"].str.contains(excl, na=False)]
        for r in g.itertuples(index=False):
            m = re.search(date_rx, r.說明 or "")
            if not m:
                continue
            y, mo, d = [int(x) for x in m.groups()[-3:]]
            try:
                dt = pd.Timestamp(year=y + 1911, month=mo, day=d)
            except ValueError:
                continue
            if lo <= dt <= hi:
                purpose = ""
                if kind.startswith("股東"):
                    pm = re.search(r"(私募|減資|增資|合併|收購|分割|股份轉換|董事改選|解除競業|變更章程|下市|終止上[市櫃]|停止公開發行|解散)", (r.主旨 or "") + (r.說明 or "")[:600])
                    purpose = pm[0] if pm else ""
                rows.append({"日期": dt, "類型": kind, "個股": r.公司代號, "名稱": names.get(r.公司代號, r.公司簡稱), "目的": purpose,
                             "熱度": float(heat.get(r.公司代號, 0)), "主旨": r.主旨, "發布時間": r.發布時間})
    df = pd.DataFrame(rows)
    return df.sort_values(["日期", "熱度"], ascending=[True, False]).drop_duplicates(["類型", "個股", "日期"]) if len(df) else df


def _heat(asof: pd.Timestamp) -> dict:
    sc = _read(SCORED, asof - pd.Timedelta(days=10), asof, "time")
    if sc.empty:
        return {}
    sc = sc[sc["stocks"].astype(str).str.fullmatch(r"\d{4}")]
    return sc.groupby("stocks")["heat"].max().to_dict()


def build(asof: pd.Timestamp, start: pd.Timestamp) -> tuple:
    lo, hi = start.normalize(), start.normalize() + pd.Timedelta(days=6)
    heat, names = _heat(asof), TN.CODE_NAMES
    ne, me = news_events(asof, lo, hi), mops_events(asof, lo, hi, heat, names)
    exp = ne[ne["預期"]].copy() if len(ne) else pd.DataFrame()
    return ne, me, exp, lo, hi


def fmt(ne: pd.DataFrame, me: pd.DataFrame, lo, hi, asof) -> str:
    lines = [f"**未來一周事件日曆 {lo:%m/%d}–{hi:%m/%d}**（{asof:%m/%d %H:%M} 整理；CPI/PMI/就業等高頻數據不列）"]
    for d in pd.date_range(lo, hi):
        day_news = ne[(ne["日期"] == d) & (ne["類型"] != "高頻數據")] if len(ne) else pd.DataFrame()
        day_m = me[me["日期"] == d] if len(me) else pd.DataFrame()
        if day_news.empty and day_m.empty:
            continue
        lines.append(f"\n**{d:%m/%d} 週{WD[d.weekday()]}**")
        shown = 0
        for r in day_news.itertuples(index=False):
            if shown >= 6:
                break
            s = re.sub(r"^(周|週)[一二三四五六日]\s*[（(][^)）]*[)）]\s*", "", r.事件)
            lines.append(f"· {s[:90]}" + ("" if r.彙整文 else f"（{r.來源}）"))
            shown += 1
        if not day_m.empty:
            for kind, g in day_m.groupby("類型"):
                hot = g[g["熱度"] >= 3].head(6)
                desc = "、".join(f"{x.名稱}{'(' + x.目的 + ')' if x.目的 else ''}" for x in hot.itertuples(index=False))
                rest = "、".join(f"{x.名稱}{'(' + x.目的 + ')' if x.目的 else ''}" for x in g[g["熱度"] < 3].head(4).itertuples(index=False))
                lines.append(f"· {kind} {len(g)} 家" + (f"｜熱門：{desc}" if desc else "") + (f"｜{rest}{'…' if len(g) > len(hot) + 4 else ''}" if rest and not desc else ""))
    exp = ne[ne["預期"] & (ne["類型"] != "高頻數據")] if len(ne) else pd.DataFrame()
    if len(exp):
        lines.append("\n**市場預期（新聞句子，非系統判斷）**")
        for r in exp.drop_duplicates("鍵").head(8).itertuples(index=False):
            lines.append(f"· {r.日期:%m/%d} {r.事件[:100]}")
    return "\n".join(lines)


def save(ne: pd.DataFrame, me: pd.DataFrame, exp: pd.DataFrame, lo: pd.Timestamp, asof: pd.Timestamp) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    cal = pd.concat([ne.assign(來源類型="新聞") if len(ne) else pd.DataFrame(), me.assign(來源類型="重訊") if len(me) else pd.DataFrame()])
    if len(cal):
        cal.assign(整理時間=asof).to_parquet(OUT / f"事件日曆_{lo:%Y-%m-%d}.parquet", index=False)
    if len(exp):
        p = OUT / f"預期_{lo:%Y-%m}.parquet"
        exp = exp.assign(整理時間=asof, 鍵2=exp["連結"].astype(str) + "|" + exp["事件"].str[:40])
        old = pd.read_parquet(p) if p.exists() else pd.DataFrame()
        pd.concat([old, exp[~exp["鍵2"].isin(old.get("鍵2", pd.Series(dtype=str)))]]).to_parquet(p, index=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", help="日曆起算日 (預設：下週一)")
    ap.add_argument("--asof", help="假裝在這個時間整理 (預設：現在)")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    asof = pd.Timestamp(a.asof) if a.asof else pd.Timestamp.now()
    if a.asof and len(a.asof) <= 10:
        asof = asof + pd.Timedelta(hours=21)
    start = pd.Timestamp(a.start) if a.start else (asof.normalize() + pd.Timedelta(days=(7 - asof.weekday()) % 7 or 7))
    ne, me, exp, lo, hi = build(asof, start)
    msg = fmt(ne, me, lo, hi, asof)
    print(msg)
    print(f"\n[新聞事件 {len(ne)} (高頻 {int((ne['類型'] == '高頻數據').sum()) if len(ne) else 0})、公司事件 {len(me)}、預期句 {len(exp)}]")
    if not a.dry:
        save(ne, me, exp, lo, asof)
        from event_rules import push_discord as P
        P.guarded_send([{"key": f"calendar-{lo:%Y-%m-%d}", "time": pd.Timestamp.now(), "score": 10.0, "text": msg}])
    return 0


if __name__ == "__main__":
    sys.exit(main())
