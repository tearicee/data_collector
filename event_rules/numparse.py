#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""數字抽取：金額 (含國字大寫、仟/萬/億單位)、百分比、股數、民國/西元日期。"""
import re

_CN = {"零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "貳": 2, "兩": 2, "三": 3, "參": 3, "叁": 3, "四": 4, "肆": 4,
       "五": 5, "伍": 5, "六": 6, "陸": 6, "七": 7, "柒": 7, "八": 8, "捌": 8, "九": 9, "玖": 9}
_UNIT = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_BIG = {"萬": 10 ** 4, "億": 10 ** 8, "兆": 10 ** 12}
_CN_CHARS = "".join(_CN) + "".join(_UNIT) + "".join(_BIG)
NUM_RE = re.compile(rf"(?:[\d,]+(?:\.\d+)?|[{_CN_CHARS}]+)(?:\s*[百佰千仟萬億兆]+)*")


def cn_to_num(s: str) -> float | None:
    """'捌億' → 8e8；'壹拾萬' → 1e5；'1.5億' → 1.5e8；'3,000' → 3000；'2仟' → 2000。"""
    s = s.replace(",", "").replace(" ", "")
    if not s:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([百佰千仟萬億兆]*)", s)
    if m:
        v = float(m[1])
        for u in m[2]:
            v *= _UNIT.get(u) or _BIG[u]
        return v
    total = section = num = 0
    for ch in s:
        if ch in _CN:
            num = _CN[ch]
        elif ch in _UNIT:
            section += (num or 1) * _UNIT[ch]
            num = 0
        elif ch in _BIG:
            total += (section + num) * _BIG[ch]
            section = num = 0
        else:
            return None
    return float(total + section + num)


MONEY_RE = re.compile(
    rf"(?P<cur>新[台臺]幣|NT\$?|美[元金]|USD|US\$|人民幣|RMB|日[圓元]|歐元|港幣)?\s*"
    rf"(?P<num>[\d,]+(?:\.\d+)?|[{_CN_CHARS}]+)\s*(?P<unit>[百佰千仟萬億兆]*)\s*"
    rf"(?P<cur2>元|美元|美金|人民幣|日圓|歐元|港幣|仟元|千元)")
_CUR = {"新台幣": "TWD", "新臺幣": "TWD", "NT": "TWD", "NT$": "TWD", "美元": "USD", "美金": "USD", "USD": "USD",
        "US$": "USD", "人民幣": "CNY", "RMB": "CNY", "日圓": "JPY", "日元": "JPY", "歐元": "EUR", "港幣": "HKD"}


def first_money(text: str, default_unit: float = 1.0):
    """抓文字中第一個金額 → (金額, 幣別)。default_unit：欄位名已註明 (仟元) 時傳 1000。"""
    for m in MONEY_RE.finditer(text or ""):
        v = cn_to_num(m["num"] + (m["unit"] or ""))
        if v is None:
            continue
        cur2 = m["cur2"]
        if cur2 in ("仟元", "千元"):
            v *= 1000
        cur = _CUR.get(m["cur"] or "", _CUR.get(cur2, "TWD"))
        return v * default_unit, cur
    return None, None


def first_number(text: str):
    """第一個數值 (含國字/單位)，無單位語意；用於股數、價格。"""
    m = re.search(rf"[\d,]+(?:\.\d+)?\s*[百佰千仟萬億兆]*|[{_CN_CHARS}]{{2,}}", text or "")
    return cn_to_num(m.group(0)) if m else None


def first_pct(text: str):
    m = re.search(r"(-?[\d,]+(?:\.\d+)?)\s*[%％]", text or "")
    return float(m[1].replace(",", "")) if m else None


def first_date(text: str) -> str:
    """'115/09/29'、'民國115年9月29日'、'2026/9/29' → '2026-09-29'。"""
    m = re.search(r"(\d{2,4})\s*[/年.\-]\s*(\d{1,2})\s*[/月.\-]\s*(\d{1,2})", text or "")
    if not m:
        return ""
    y = int(m[1])
    y = y + 1911 if y < 1911 else y
    return f"{y:04d}-{int(m[2]):02d}-{int(m[3]):02d}"
