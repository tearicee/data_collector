#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FinLab 重大訊息歷史 → 重訊主庫 (來源=finlab)
================================================================
  finlab.login(token) + data.get('important_info_announcement')  (finlab==1.5.7，token 登入不開瀏覽器)
  金鑰：~/daily_script/.env 的 FINLAB_API_KEY (不要寫進程式或 log)
  資料：2006 起全市場 (上市/上櫃/興櫃/公開發行)，欄位 stock_id / date(發布時間，到秒) / name / title / info

匯入規則：
  - 只補主庫沒有的 (以「公司代號|發布時間」比對；主庫既有的 MOPS/ES 匯出列一律不覆寫，它們的說明保有原始換行)。
  - FinLab 的 info 把換行全部拿掉了 →「1.xxx2.yyy」。restore_lines() 依欄位編號 1. 2. 3. … 依序還原換行，
    讓 extract_events.parse_fields 抽得到欄位；表格 (自結財務表) 的版面無法還原，這類數據抽不到屬正常。
  - 來源=finlab、MOPS鍵/市場別/符合條款 空白；事實發生日取自說明第 1 欄。
  - 一次下載約用 1.4 GB 流量 (每日額度 5 GB) → 原始檔快取在 finlab/important_info_announcement_raw.pkl，
    預設沿用快取；要重新下載加 --refresh。

用法：
  python material_info/import_finlab.py              # 用快取 (沒有才下載) 匯入全部缺漏
  python material_info/import_finlab.py --refresh    # 重新向 FinLab 下載後匯入
  python material_info/import_finlab.py --dry        # 只印統計不寫入
  python material_info/import_finlab.py --start 2025-01-01   # 只匯入此日之後
"""
import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store  # noqa: E402

DATASET = "important_info_announcement"
RAW = store.BASE_DIR / "finlab" / f"{DATASET}_raw.pkl"
ENV_FILE = Path("/home/tearicee/daily_script/.env")
FACT_DAY = re.compile(r"事實發生日[:：]\s*(\d{2,3})/(\d{1,2})/(\d{1,2})")


def download() -> pd.DataFrame:
    from dotenv import dotenv_values
    token = (dotenv_values(ENV_FILE).get("FINLAB_API_KEY") or "").strip()
    if not token:
        raise SystemExit(f"{ENV_FILE} 找不到 FINLAB_API_KEY")
    import finlab
    from finlab import data
    finlab.login(token)
    df = pd.DataFrame(data.get(DATASET))
    RAW.parent.mkdir(parents=True, exist_ok=True)
    df.to_pickle(RAW)
    return df


def restore_lines(info: str) -> str:
    """「1.事實發生日:115/09/292.公司名稱:…」→ 每個編號欄位各一行。
    從第一個「1.」起依序找 2. 3. 4. …：編號後面不能接數字 (排除 2.5 億這種小數)，找不到下一號就停。"""
    if not info or "\n" in info:
        return info or ""
    first = re.search(r"1[.、](?=[^\d\s.、]|\d{1,2}月)", info)
    if not first:
        return info
    out, pos, k = [info[:first.start()]] if first.start() else [], first.start(), 2
    while True:
        m = re.compile(rf"{k}[.、](?=[^\d\s.、]|\d{{1,2}}月)").search(info, pos + 2)
        if not m:
            break
        out.append(info[pos:m.start()])
        pos, k = m.start(), k + 1
    out.append(info[pos:])
    return "\n".join(s.strip() for s in out)


def to_rows(df: pd.DataFrame) -> pd.DataFrame:
    now = datetime.now().strftime(store.TS_FMT)
    t = pd.to_datetime(df["date"])
    info = df["info"].fillna("").astype(str)
    fact = info.str.slice(0, 80).str.extract(FACT_DAY)
    ok = fact[0].notna()
    fact_day = pd.Series("", index=df.index)
    fact_day[ok] = ((fact.loc[ok, 0].astype(int) + 1911).astype(str) + "-" + fact.loc[ok, 1].str.zfill(2)
                    + "-" + fact.loc[ok, 2].str.zfill(2))
    return pd.DataFrame({
        "公司代號": df["stock_id"].astype(str), "公司簡稱": df["name"].fillna("").astype(str),
        "發布時間": t.dt.strftime(store.TS_FMT), "主旨": df["title"].fillna("").astype(str),
        "說明": info.map(restore_lines), "市場別": "", "發言日期": t.dt.strftime("%Y-%m-%d"),
        "發言時間": t.dt.strftime("%H:%M:%S"), "符合條款": "", "事實發生日": fact_day,
        "發言人": "", "發言人職稱": "", "發言人電話": "", "來源": "finlab", "MOPS鍵": "", "抓取時間": now,
    })


def main() -> int:
    ap = argparse.ArgumentParser(description="FinLab 重大訊息歷史匯入")
    ap.add_argument("--refresh", action="store_true", help="重新向 FinLab 下載 (約 1.4 GB 流量)")
    ap.add_argument("--dry", action="store_true", help="只印統計不寫入")
    ap.add_argument("--start", help="只匯入此發言日期之後 (YYYY-MM-DD)")
    a = ap.parse_args()
    df = download() if a.refresh or not RAW.exists() else pd.DataFrame(pd.read_pickle(RAW))
    df = df.dropna(subset=["date", "stock_id"])
    if a.start:
        df = df[df["date"] >= pd.Timestamp(a.start)]
    rows = to_rows(df)
    print(f"FinLab {len(rows):,} 筆 ({rows['發言日期'].min()} ~ {rows['發言日期'].max()})")
    added = 0
    for ym, g in rows.groupby(rows["發言日期"].str[:7]):
        have = {f"{r['公司代號']}|{r['發布時間']}" for r in store.load_month(ym)}
        new = g[~(g["公司代號"] + "|" + g["發布時間"]).isin(have)].drop_duplicates(["公司代號", "發布時間"])
        if len(new) and not a.dry:
            store.upsert(new.to_dict("records"))
        added += len(new)
    print(f"{'(dry) ' if a.dry else ''}補入 {added:,} 筆；其餘 {len(rows) - added:,} 筆主庫已有")
    return 0


if __name__ == "__main__":
    sys.exit(main())
