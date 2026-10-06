#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重大訊息過濾 (規則式，不用 LLM) — 只標記不刪除，原始庫不動
================================================================
每筆重訊貼 filter_label：
  repeat     重發：同公司且「主旨+說明全文」雜湊與更早一筆相同，或主旨帶「公告期間：A至B」
             且發言日期晚於 A (更名/面額變更 3 個月連續公告，每天 07:00 系統重發)。
             只留首發，其餘記 repeat_of = 首發 MOPS鍵。
  routine    例行：人事異動、背書保證/資金貸與、法說會日期、例行理財/有價證券買賣…
  candidate  其餘，進入後續評分。

公司行動 (面額變更/減資/更名/分割) 另標 action_type / action_stage，同一主題的各階段
公告說明內容不同，不會被 repeat 吃掉：
  1-董事會/股東會決議  2-主管機關核准  3-日程公布(作業計畫/基準日)
  4-變更登記完成/恢復買賣  連續公告(重發)
更正/補充公告 is_correction=True，一律保留為 candidate。

輸出：/mnt/d/mops/material_info/derived/重訊篩選_YYYY-MM.parquet
用法：
  python filter_material_info.py                 # 重算全部月份 (主庫自 2006 起約 126 萬筆，需數分鐘)
  python filter_material_info.py --recent 2      # 只重算最近 2 個月 (每日流程用；判重往回多讀 4 個月)
  python filter_material_info.py --summary       # 只印統計不寫檔
"""

import argparse
import hashlib
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store  # noqa: E402

DERIVED_DIR = store.BASE_DIR / "derived"
LOOKBACK_MONTHS = 4  # 連續公告最長 3 個月，判重要往回看

ACTION_TYPES = [  # (action_type, 主旨 regex)；順序即優先序
    ("面額變更", r"面額"),
    ("更名", r"名稱由|更名"),
    ("減資", r"減資"),
    ("分割", r"分割"),
]
STAGE_RULES = [  # (stage, 主旨 regex)；依序比對，先中先贏
    ("4-變更登記完成/恢復買賣", r"變更登記|恢復買賣|新股.{0,6}(上市|上櫃)買賣"),
    ("2-主管機關核准", r"業經.{0,6}核准|核准事宜|申報生效"),
    ("3-日程公布", r"作業計[畫劃]|換發|換股|換票|基準日|最後交易日|停止轉換"),
    ("1-董事會/股東會決議", r"董事會|股東(常|臨時)?會|董事長"),
]
ROUTINE_RULES = [  # (原因, 主旨 regex)
    ("人事異動", r"(發言人|代理發言人|財務主管|會計主管|稽核主管|資訊安全長|資安長|公司治理主管|研發主管)"
                 r".{0,8}(異動|變動)|(異動|變動).{0,6}(發言人|主管)"),
    ("背書保證/資金貸與", r"背書保證|資金貸與"),
    ("法說會資訊", r"法人說明會|法說會|受邀參加|業績發表會|投資論壇"),
    ("例行理財/有價證券", r"理財(產品|商品)|結構式存款|定期存款|(取得|處分|買賣).{0,4}(有價證券|基金|債券|公司債|受益憑證)"),
    ("股東會/董事會程序", r"股東(常|臨時)?會.{0,6}(召開|日期|相關事宜|受理)|停止過戶"),
    ("財報提報程序", r"財務報(告|表).{0,12}(提報|通過|董事會)"),
    ("每月資金/財務比率申報", r"現金收支|融資額度.{0,8}使用|自結.{0,12}(負債比率|流動比率)|資金調度|背書保證.{0,6}餘額"),
    ("買回執行進度", r"買回股份.{0,12}(金額達|累積|期間屆滿)"),
]
PERIOD_RE = re.compile(r"公告期間[：:]\s*(\d{2,3})年(\d{1,2})月(\d{1,2})日")
CORRECTION_RE = re.compile(r"更正|補充|更新")


def _content_hash(text: str) -> str:
    return hashlib.md5(re.sub(r"\s+", "", text or "").encode("utf-8")).hexdigest()


def _period_start(subject: str) -> str:
    m = PERIOD_RE.search(subject)
    return f"{int(m[1]) + 1911:04d}-{int(m[2]):02d}-{int(m[3]):02d}" if m else ""


def _first_match(rules, subject: str) -> str:
    return next((name for name, pat in rules if re.search(pat, subject)), "")


def classify(df: pd.DataFrame) -> pd.DataFrame:
    """輸入重訊 DataFrame (需含較早月份供判重)，回傳加上過濾欄位的 DataFrame。"""
    df = df.sort_values(["發布時間", "MOPS鍵"]).reset_index(drop=True)
    subj = df["主旨"].fillna("")
    # 主旨一併納入雜湊：不同子公司常共用一字不差的說明 (如兩家子公司各自決議盈餘分配)
    df["content_hash"] = [_content_hash(f"{a}|{b}") for a, b in zip(subj, df["說明"].fillna(""))]
    df["is_correction"] = subj.str.contains(CORRECTION_RE)
    df["action_type"] = [_first_match(ACTION_TYPES, s) for s in subj]

    # --- repeat：同公司同內容的第 2 筆起；或連續公告期間內非首日
    first_key = df.groupby(["公司代號", "content_hash"])["MOPS鍵"].transform("first")
    dup = df.duplicated(["公司代號", "content_hash"], keep="first") & (df["說明"].fillna("") != "")
    period = subj.map(_period_start)
    in_period = (period != "") & (df["發言日期"] > period)
    df["repeat_of"] = ""
    df.loc[dup, "repeat_of"] = first_key[dup]
    df.loc[in_period & ~dup, "repeat_of"] = "(首發早於資料庫起始日)"
    is_repeat = dup | in_period

    # --- 公司行動階段
    stage = [(_first_match(STAGE_RULES, s) or "其他") if a else ""
             for s, a in zip(subj, df["action_type"])]
    df["action_stage"] = stage
    df.loc[(period != "") & (df["action_type"] != ""), "action_stage"] = "連續公告"
    df["action_id"] = [f"{c}-{a}" if a else "" for c, a in zip(df["公司代號"], df["action_type"])]

    # --- routine (公司行動與更正不列入)
    routine = [_first_match(ROUTINE_RULES, s) for s in subj]
    df["routine_reason"] = routine
    is_routine = (df["routine_reason"] != "") & (df["action_type"] == "") & ~df["is_correction"]
    df.loc[~is_routine, "routine_reason"] = ""

    df["filter_label"] = "candidate"
    df.loc[is_routine, "filter_label"] = "routine"
    df.loc[is_repeat, "filter_label"] = "repeat"
    return df


def recent_window(recent: int) -> tuple:
    """最近 N 個月 → (要寫出的月份清單, 判重需要讀取的起始日)。"""
    now = pd.Timestamp.now()
    months = [(now - pd.DateOffset(months=i)).strftime("%Y-%m") for i in range(recent)]
    start = (pd.Period(min(months), "M") - LOOKBACK_MONTHS).strftime("%Y-%m-01")
    return months, start


def run(write: bool = True, recent: int = 0) -> pd.DataFrame:
    months, start = recent_window(recent) if recent else (None, None)
    df = classify(store.read_range(start))
    if months:
        df = df[df["發言日期"].str[:7].isin(months)]
    if write:
        DERIVED_DIR.mkdir(parents=True, exist_ok=True)
        for ym, g in df.groupby(df["發言日期"].str[:7]):
            path = DERIVED_DIR / f"重訊篩選_{ym}.parquet"
            tmp = path.with_suffix(".parquet.tmp")
            g.drop(columns=["發言人", "發言人職稱", "發言人電話"]).to_parquet(
                tmp, compression="zstd", index=False)
            tmp.replace(path)
    return df


def main() -> int:
    ap = argparse.ArgumentParser(description="重大訊息規則過濾")
    ap.add_argument("--summary", action="store_true", help="只印統計不寫檔")
    ap.add_argument("--recent", type=int, default=0, help="只重算最近 N 個月 (每日流程用)；0 = 全部")
    args = ap.parse_args()
    df = run(write=not args.summary, recent=args.recent)
    print(f"總筆數 {len(df):,}")
    print(df["filter_label"].value_counts().to_string())
    print("\nroutine 原因：")
    print(df.loc[df["filter_label"] == "routine", "routine_reason"].value_counts().to_string())
    act = df[df["action_type"] != ""]
    print("\n公司行動 × 階段 × 標籤：")
    print(pd.crosstab([act["action_type"], act["action_stage"]], act["filter_label"]).to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
