#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重訊事件標籤 + 數據抽取 (純規則，不經 AI 模型)
================================================================
1. parse_fields()：重訊「說明」是編號欄位表單 (1.董事會決議日期:115/09/29 …)，拆成 {欄位名: 值}。
2. 事件標籤：event_rules/rules.py (主旨正則 + 欄位名特徵)，一則可多標籤。
3. 數據抽取：現金增資 / 私募 / 可轉債·公司債 / 減資 / 自結 / 庫藏股，依欄位名取值並轉成數字。
   金額一律換算為「元」，另存幣別；抽不到留空，原文仍在重訊庫。

輸出：/mnt/d/mops/material_info/derived/重訊事件_YYYY-MM.parquet
  MOPS鍵、公司代號、公司簡稱、發布時間、主旨、filter_label、
  tags / modifiers (以 | 分隔)、entities (JSON)、數據 (JSON)、金額_元、幣別
用法：python extract_events.py [--show 現金增資]
"""
import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import filter_material_info as fmi  # noqa: E402
import store  # noqa: E402
from event_rules import numparse as N  # noqa: E402
from event_rules.rules import tag_text  # noqa: E402

FIELD_RE = re.compile(r"(?m)^\s*(\d{1,2})[\.、]\s*([^:：\n]{2,60}?)\s*[:：]")
NA_RE = re.compile(r"^\s*(不適用|無|NA|N/A|none|尚未|待定)[。.]?\s*$", re.I)


def parse_fields(text: str) -> dict:
    parts = FIELD_RE.split(text or "")
    out = {}
    for i in range(1, len(parts) - 2, 3):
        out.setdefault(parts[i + 1].strip(), parts[i + 2].strip())
    return out


def pick(fields: dict, *needles) -> str:
    """取第一個欄位名含任一關鍵字且值非「不適用」者。"""
    for nd in needles:
        for k, v in fields.items():
            if nd in k and v and not NA_RE.match(v.split("\n")[0]):
                return v
    return ""


def _money(d: dict, name: str, text: str, unit: float = 1.0) -> None:
    v, cur = N.first_money(text, unit)
    if v is None and text:  # 'USD23,000,000' 這類無「元」字者
        m = re.search(r"(USD|US\$|美金|NTD?\$?|RMB|人民幣)\s*([\d,]+(?:\.\d+)?)", text)
        if m:
            v, cur = float(m[2].replace(",", "")), N._CUR.get(m[1], "USD" if "U" in m[1] else "TWD")
    if v is not None:
        d[name], d[name + "_幣別"] = v, cur


def _shares(text: str):
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*(仟|千|萬|億)?\s*股", text or "")
    if not m:
        return None
    return float(m[1].replace(",", "")) * {"仟": 1e3, "千": 1e3, "萬": 1e4, "億": 1e8}.get(m[2], 1)


def _set(d, name, val):
    if val not in (None, ""):
        d[name] = val


def x_cash_increase(f: dict, body: str) -> dict:
    d = {}
    total = pick(f, "全案發行總金額及股數", "本次發行金額及股數")
    _set(d, "董事會決議日", N.first_date(pick(f, "董事會決議日期")))
    _money(d, "發行總金額", total)
    _set(d, "發行股數", _shares(total))
    _money(d, "每股面額", pick(f, "每股面額"))
    _money(d, "發行價格", pick(f, "發行價格"))
    _set(d, "公開銷售股數", _shares(pick(f, "公開銷售")))
    _set(d, "資金用途", pick(f, "本次增資資金用途").split("\n")[0][:80])
    if "發行總金額" not in d and d.get("發行股數") and d.get("發行價格"):
        d["發行總金額"], d["發行總金額_幣別"] = d["發行股數"] * d["發行價格"], d.get("發行價格_幣別", "TWD")
    return d


def x_private(f: dict, body: str) -> dict:
    d = {}
    if not pick(f, "私募有價證券種類"):  # 認購他人私募 (資產取得表單)，非發行方
        return d
    _set(d, "董事會決議日", N.first_date(pick(f, "董事會決議日期")))
    _set(d, "證券種類", pick(f, "私募有價證券種類")[:30])
    who = pick(f, "私募對象及其與公司間關係")
    _set(d, "應募人摘要", re.sub(r"\s+", " ", who)[:200])
    _set(d, "私募股數", _shares(pick(f, "私募股數或張數")) or N.first_number(pick(f, "私募股數或張數")))
    _money(d, "參考價格", pick(f, "參考價格"))
    _money(d, "私募價格", pick(f, "實際私募價格"))
    _set(d, "定價日", N.first_date(pick(f, "實際定價日")))
    _set(d, "資金用途", pick(f, "本次私募資金用途").split("\n")[0][:80])
    if d.get("參考價格") and d.get("私募價格"):
        d["折價率_pct"] = round((1 - d["私募價格"] / d["參考價格"]) * 100, 2)
    if d.get("私募股數") and d.get("私募價格"):
        d["私募總金額"], d["私募總金額_幣別"] = d["私募股數"] * d["私募價格"], d.get("私募價格_幣別", "TWD")
    return d


def x_bond(f: dict, body: str) -> dict:
    d = {}
    _set(d, "董事會決議日", N.first_date(pick(f, "董事會決議日期")))
    _set(d, "債券名稱", pick(f, "名稱﹝").split("\n")[0][:60])
    _money(d, "發行總額", pick(f, "發行總額"))
    _money(d, "每張面額", pick(f, "每張面額"))
    _set(d, "發行價格說明", pick(f, "發行價格").split("\n")[0][:60])
    _set(d, "發行期間", pick(f, "發行期間").split("\n")[0][:20])
    _set(d, "票面利率_pct", N.first_pct(pick(f, "發行利率")))
    _set(d, "資金用途", pick(f, "募得價款之用途").split("\n")[0][:80])
    _set(d, "承銷方式", pick(f, "承銷方式").split("\n")[0][:40])
    if "發行總額" not in d:  # 海外債訂價等寫在其他應敘明事項
        m = re.search(r"(募集|發行).{0,12}總額[：:]\s*([^\n]{4,40})", body)
        if m:
            unit = 1000 if "仟" in m[2] or "千" in m[2] else 1
            _money(d, "發行總額", m[2].replace("仟", "").replace("千", "") + ("元" if "元" not in m[2] else ""), unit)
        m = re.search(r"轉換價格[^\n]{0,20}?([\d,]+(?:\.\d+)?)\s*元", body)
        if m:
            d["轉換價格"] = float(m[1].replace(",", ""))
        _set(d, "轉換溢價率_pct", N.first_pct((re.search(r"溢價[^\n]{0,30}", body) or [""])[0]))
    return d


def x_reduction(f: dict, body: str) -> dict:
    d = {}
    _set(d, "董事會決議日", N.first_date(pick(f, "董事會決議日期")))
    _set(d, "減資緣由", pick(f, "減資緣由").split("\n")[0][:60])
    _money(d, "減資金額", pick(f, "減資金額"))
    _set(d, "消除股數", _shares(pick(f, "消除股份")))
    _set(d, "減資比率_pct", N.first_pct(pick(f, "減資比率")))
    _money(d, "減資後股本", pick(f, "減資後股本"))
    _set(d, "減資基準日", N.first_date(pick(f, "減資基準日")))
    _set(d, "核准日", N.first_date(pick(f, "主管機關核准減資日期")))
    _set(d, "變更登記完成日", N.first_date(pick(f, "辦理資本變更登記完成日期")))
    _set(d, "舊股最後交易日", N.first_date(pick(f, "舊股票最後交易日")))
    _set(d, "換發基準日", N.first_date(pick(f, "換發股票基準日")))
    _set(d, "新股上市日", N.first_date(pick(f, "新股上市日期", "新股上櫃日期")))
    kind = "現金減資" if "現金減資" in body[:400] or "退還股款" in body else (
        "彌補虧損" if "彌補虧損" in body else ("註銷庫藏股/限制員工新股" if re.search(r"註銷|庫藏股|限制員工", body[:600]) else ""))
    _set(d, "減資類型", kind)
    return d


_ROW = {"營業收入": "營收", "稅前淨利": "稅前淨利", "稅前損益": "稅前淨利", "司業主淨利": "母公司淨利",
        "業主淨利": "母公司淨利", "本期淨利": "母公司淨利", "每股盈餘": "EPS", "每股虧損": "EPS"}


def x_self_report(f: dict, body: str) -> dict:
    """注意交易資訊的財務業務資訊表：每列 5 個數 = 最近一月、年增%、最近一季、年增%、近四季累計。"""
    d = {}
    seg = body[body.find("財務業務資訊"):] if "財務業務資訊" in body else body
    unit = "仟元" if re.search(r"仟元|千元", seg[:900]) else ("百萬" if "百萬" in seg[:900] else "")
    for line in seg.split("\n"):
        key = next((v for k, v in _ROW.items() if k in line), None)
        nums = re.findall(r"-?\(?[\d,]+(?:\.\d+)?\)?", re.sub(r"^[^\d\-(]*", "", line))
        if not key or len(nums) < 3 or f"{key}_最近一月" in d:
            continue
        vals = [(-1 if n.startswith("(") or n.startswith("-(") else 1) * float(n.strip("()-").replace(",", "") or 0)
                * (-1 if n.startswith("-") and not n.startswith("-(") else 1) for n in nums[:5]]
        for name, v in zip(["最近一月", "月年增_pct", "最近一季", "季年增_pct", "近四季累計"], vals):
            d[f"{key}_{name}"] = v
    m = re.search(r"(\d{2,3})年\s*(\d{1,2})月", seg)
    if d:
        _set(d, "金額單位", unit)
        _set(d, "資料月份", f"{int(m[1]) + 1911}-{int(m[2]):02d}" if m else "")
    for k in ("自結負債比率", "自結流動比率", "自結速動比率"):
        _set(d, k.replace("自結", "") + "_pct", N.first_pct(pick(f, k)) or N.first_number(pick(f, k)))
    return d


def x_buyback(f: dict, body: str) -> dict:
    d = {}
    _set(d, "預定買回股數", _shares(pick(f, "原預定買回之數量", "預定買回之數量") + "股"))
    _money(d, "預定買回金額上限", pick(f, "買回股份總金額上限") + "元")
    _set(d, "買回區間說明", pick(f, "買回區間價格").split("\n")[0][:40])
    _set(d, "本次買回股數", _shares(pick(f, "本次買回股份數量(股)", "本次已買回股份數量") + "股"))
    _money(d, "本次買回金額", pick(f, "本次買回股份總金額", "本次已買回股份總金額") + "元")
    _set(d, "平均買回價格", N.first_number(pick(f, "平均每股買回價格")))
    _set(d, "累積持股比率_pct", N.first_number(pick(f, "占公司已發行股份總數之比率")))
    _set(d, "買回目的", pick(f, "買回股份目的").split("\n")[0][:30])
    return d


EXTRACTORS = [  # (標籤, 抽取函式, 主要金額欄)
    ("現金增資", x_cash_increase, "發行總金額"), ("私募", x_private, "私募總金額"),
    ("可轉債", x_bond, "發行總額"), ("公司債", x_bond, "發行總額"),
    ("減資", x_reduction, "減資金額"), ("自結", x_self_report, None), ("庫藏股", x_buyback, "本次買回金額"),
]


def build() -> pd.DataFrame:
    df = fmi.classify(store.read_range())
    rows = []
    for r in df.itertuples(index=False):
        body = r.說明 or ""
        fields = parse_fields(body)
        t = tag_text(r.主旨 or "", body, fields.keys(), source="mops")
        data, amount, cur = {}, None, ""
        for tag, fn, main in EXTRACTORS:
            if tag in t["tags"]:
                got = fn(fields, body)
                if got:
                    data[tag] = got
                    if main and amount is None and got.get(main):
                        amount, cur = got[main], got.get(main + "_幣別", "TWD")
        rows.append({
            "MOPS鍵": r.MOPS鍵, "公司代號": r.公司代號, "公司簡稱": r.公司簡稱, "市場別": r.市場別,
            "發布時間": r.發布時間, "發言日期": r.發言日期, "主旨": r.主旨,
            "filter_label": r.filter_label, "action_stage": r.action_stage,
            "tags": "|".join(t["tags"]), "modifiers": "|".join(t["modifiers"]),
            "entities": json.dumps(t["entities"], ensure_ascii=False) if t["entities"] else "",
            "數據": json.dumps(data, ensure_ascii=False) if data else "",
            "金額_元": amount, "幣別": cur,
        })
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", help="列出某標籤的抽取結果")
    a = ap.parse_args()
    out = build()
    fmi.DERIVED_DIR.mkdir(parents=True, exist_ok=True)
    for ym, g in out.groupby(out["發言日期"].str[:7]):
        path = fmi.DERIVED_DIR / f"重訊事件_{ym}.parquet"
        tmp = path.with_suffix(".parquet.tmp")
        g.to_parquet(tmp, compression="zstd", index=False)
        tmp.replace(path)
    cand = out[out["filter_label"] != "repeat"]
    n_tag = cand["tags"].str.split("|").map(lambda x: len([i for i in x if i]))
    print(f"總 {len(out):,}；非重發 {len(cand):,}：無標籤 {(n_tag == 0).sum()}、單標籤 {(n_tag == 1).sum()}、多標籤 {(n_tag > 1).sum()}")
    print(cand["tags"].str.split("|").explode().replace("", "(無標籤)").value_counts().to_string())
    print("\n數據抽取筆數：")
    for tag, _, main in EXTRACTORS:
        s = cand[cand["tags"].str.split("|").map(lambda x: tag in x)]
        has = s["數據"].map(lambda j: tag in json.loads(j) if j else False)
        print(f"  {tag}: 標籤 {len(s)} 筆，有抽到數據 {has.sum()} 筆")
    if a.show:
        s = cand[cand["數據"].map(lambda j: a.show in json.loads(j) if j else False)]
        for r in s.head(12).itertuples():
            print(f"\n{r.發言日期} {r.公司代號} {r.公司簡稱} | {r.主旨[:46]}\n   {json.loads(r.數據)[a.show]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
