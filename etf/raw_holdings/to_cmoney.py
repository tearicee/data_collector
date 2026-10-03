#!/usr/bin/env python3
"""投信原始持股 → CMoney 格式持股檔 (取代已失效的 CMoney 爬蟲)。

背景: CMoney 的 GetDtnoData 端點自 2026-10-01 起回 {"Error":{"Code":101,"Message":"Auth Failed"}}，
etf_crawler.py 全數失敗。下游 (daily_script 的 ETF 異動 / 除息預告、健檢、備份、研究腳本) 都讀
D:\\etf_daily_holdings\\data\\<YYYYMMDD>_<etf>.csv，故改由投信官網原始持股 (run_raw.py 的輸出) 轉成
同一格式寫回同一目錄，下游不必改。

格式對照 (已用 2026-09 兩邊重疊的資料逐檔比對，個股股數與權重完全一致):
    CMoney      date, stock_id, weight(%), holdings(張 = 股數/1000), etf
    投信原始    date, fund_code, stock_id, name, asset_type, shares(股), weight
  - holdings = shares / 1000；weight(%) 取到小數 2 位 (CMoney 的精度；投信有的給到 4 位)。
  - 期貨代號不同: CMoney 帶合約月份 (202610TX)，投信只有商品代號 (TX)。照投信的寫。
  - CMoney 另有現金列 (C_NTD / M_NTD …)，投信沒有。下游只取個股列，不受影響。

日期 —— 不能用投信檔名的日期:
  CMoney 的 date 是「持股基準日」。投信檔名日期各家不同，而且有的會隨執行時間變:
    元大/國泰/統一/復華/野村/大華   來源自帶日期，與 CMoney 同日
    群益/兆豐/凱基                  來源自帶日期，但標的是下一個交易日 (申購買回清單適用日)
    中國信託/台新/新光/富邦          用執行當下的日期；過了午夜才跑就會標成隔天
  所以一律由內容判定: 用「股數 × 某日收盤價」重算權重，與檔內權重最吻合的交易日就是持股基準日。
  這個判定以 2026-09-15~09-30 共 665 個有 CMoney 對照的檔驗證，全數正確
  (正確日誤差最大 0.52%，其他日最小 0.37% 且通常 >1%；見 --validate)。
  判定不出來的 (國內個股不足 MIN_STOCKS 檔，如槓桿/反向/海外/債券型；或吻合度不夠) 不轉檔，寧缺勿錯。

只寫 CMONEY_LAST_DATE 之後的日期，CMoney 時期抓到的原檔不覆寫。

用法:
  python raw_holdings/to_cmoney.py                    # 轉最新交易日前 RECENT_DAYS 天以內的投信檔
  python raw_holdings/to_cmoney.py --since 20261001   # 回補: 指定投信檔名日期下限
  python raw_holdings/to_cmoney.py --dry-run          # 只印不寫
  python raw_holdings/to_cmoney.py --require-latest   # 最新交易日轉出檔數不足時 exit 1 (排程最後一輪用)
  python raw_holdings/to_cmoney.py --validate         # 與 CMoney 原檔比對 (日期判定 + 欄位數值)
"""
from __future__ import annotations
import argparse
import glob
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

if sys.platform == "win32":
    RAW_DIR = Path(r"D:\etf_daily_holdings_raw")
    OUT_DIR = Path(r"D:\etf_daily_holdings\data")
    PRICE_DIR = Path(r"D:\finmind_data\TaiwanStockPrice")
else:
    RAW_DIR = Path("/mnt/d/etf_daily_holdings_raw")
    OUT_DIR = Path("/mnt/d/etf_daily_holdings/data")
    PRICE_DIR = Path("/mnt/d/finmind_data/TaiwanStockPrice")

CMONEY_LAST_DATE = "20260930"    # CMoney 最後一個抓到的資料日；此日(含)以前的檔不動
CMONEY_COLUMNS = ["date", "stock_id", "weight(%)", "holdings", "etf"]

MIN_STOCKS = 8                   # 至少幾檔國內個股才判定日期
MAX_FIT_ERR = 0.006              # 最佳日的總權重誤差比例上限
MIN_MARGIN = 1.5                 # 次佳日誤差 / 最佳日誤差 的下限
LOOKBACK_TRADING_DAYS = 6        # 候選: 檔名日期(含)往前幾個交易日
RECENT_DAYS = 10                 # 預設只看檔名日期在最新交易日前幾天以內的投信檔
MIN_PRICE_ROWS = 500             # 價檔列數低於此視為非交易日/殘檔
MIN_LATEST_FILES = 30            # --require-latest: 最新交易日至少要轉出幾檔

# 檔名日期 = 來源自帶的持股基準日 的發行商。它們出現比最新價檔還新的日期，代表當日價檔缺漏；
# 此時當日持股會被誤判成前一交易日 (少數情況誤差仍在門檻內)，所以整批不轉。
SAME_DAY_ISSUERS = {"元大", "國泰", "統一", "復華", "野村", "大華"}

STOCK_RE = re.compile(r"^\d{4,6}[A-Z]?$")       # 與 daily_script src/core/etf_holdings.py 相同


@dataclass
class Fit:
    date: str | None      # 判定的持股基準日；None = 判定不出來
    reason: str           # ok / few_stocks / no_price / poor_fit / ambiguous
    err: float = float("nan")
    margin: float = float("nan")


def load_prices(price_dir: Path, upto: str | None = None, days: int = 40) -> dict[str, pd.Series]:
    """讀最近 `days` 個交易日的收盤價 {YYYYMMDD: Series(stock_id -> close)}。有價檔且列數夠才算交易日。"""
    files = []
    for f in glob.glob(str(price_dir / "*" / "TaiwanStockPrice_*.parquet")):
        d = Path(f).stem.split("_", 1)[1].replace("-", "")
        if d.isdigit() and len(d) == 8 and (upto is None or d <= upto):
            files.append((d, f))
    out: dict[str, pd.Series] = {}
    for d, f in sorted(files)[-days:]:
        df = pd.read_parquet(f, columns=["stock_id", "close"])
        if len(df) < MIN_PRICE_ROWS:
            continue
        s = pd.to_numeric(df.drop_duplicates("stock_id").set_index("stock_id")["close"], errors="coerce")
        out[d] = s[s > 0]
    return out


def read_raw(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"stock_id": str, "fund_code": str}, encoding="utf-8-sig")
    df["stock_id"] = df["stock_id"].astype(str).str.strip()
    df["shares"] = pd.to_numeric(df["shares"], errors="coerce")
    df["weight"] = pd.to_numeric(df["weight"], errors="coerce")
    return df


def _domestic(df: pd.DataFrame) -> pd.DataFrame:
    d = df[(df["asset_type"].fillna("股票") == "股票") & df["stock_id"].str.match(STOCK_RE)]
    d = d[(d["shares"] > 0) & (d["weight"] > 0)]
    return d.drop_duplicates("stock_id").set_index("stock_id")


def valuation_date(df: pd.DataFrame, label: str, prices: dict[str, pd.Series]) -> Fit:
    """由內容判定持股基準日: 股數×收盤價重算的權重與檔內權重最吻合的交易日。"""
    dom = _domestic(df)
    if len(dom) < MIN_STOCKS:
        return Fit(None, "few_stocks")
    candidates = [d for d in sorted(prices) if d <= label][-LOOKBACK_TRADING_DAYS:]
    errs: list[tuple[float, str]] = []
    for d in candidates:
        p = prices[d].reindex(dom.index)
        ok = p.notna()
        if ok.sum() < max(MIN_STOCKS, 0.8 * len(dom)):
            continue
        mv = dom.loc[ok, "shares"] * p[ok]
        w = dom.loc[ok, "weight"]
        calc = mv / mv.sum() * w.sum()
        errs.append((float((calc - w).abs().sum() / w.sum()), d))
    if not errs:
        return Fit(None, "no_price")
    errs.sort()
    best_err, best = errs[0]
    margin = (errs[1][0] / max(best_err, 1e-9)) if len(errs) > 1 else float("inf")
    if best_err > MAX_FIT_ERR:
        return Fit(None, "poor_fit", best_err, margin)
    if margin < MIN_MARGIN:
        return Fit(None, "ambiguous", best_err, margin)
    return Fit(best, "ok", best_err, margin)


def to_cmoney(df: pd.DataFrame, etf: str, date: str) -> pd.DataFrame:
    """投信正規化欄位 → CMoney 欄位。保留投信給的所有列 (含期貨/海外標的)，下游自行篩個股。"""
    out = pd.DataFrame({
        "date": date,
        "stock_id": df["stock_id"],
        "weight(%)": df["weight"].round(2),
        "holdings": (df["shares"] / 1000).round(3),
        "etf": etf,
    })
    out = out[out["stock_id"].ne("") & out["stock_id"].ne("nan") & out["holdings"].notna()]
    return out[CMONEY_COLUMNS].reset_index(drop=True)


def raw_files(raw_dir: Path, since: str) -> list[tuple[str, str, str, Path]]:
    """回傳 [(發行商, 檔名日期, ETF 代號, 路徑)]，檔名日期 >= since。"""
    out = []
    for f in glob.glob(str(raw_dir / "*" / "*_*.csv")):
        p = Path(f)
        if p.parent.name.startswith("_") or p.parent.name == "logs":
            continue
        label, _, etf = p.stem.partition("_")
        if label.isdigit() and len(label) == 8 and etf and label >= since:
            out.append((p.parent.name, label, etf, p))
    return sorted(out)


def _write(df: pd.DataFrame, path: Path) -> bool:
    """內容有變才寫 (先寫暫存檔再換名)。回傳是否寫入。"""
    text = df.to_csv(index=False)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8-sig") == text:
                return False
        except Exception:                                # noqa: BLE001
            pass
    tmp = path.with_suffix(".csv.tmp")
    tmp.write_text(text, encoding="utf-8-sig")
    os.replace(tmp, path)
    return True


def convert(raw_dir: Path, out_dir: Path, price_dir: Path, since: str | None = None,
            dry_run: bool = False, verbose: bool = True) -> dict:
    prices = load_prices(price_dir)
    if not prices:
        raise RuntimeError(f"讀不到收盤價 ({price_dir})，無法判定持股基準日")
    latest_td = max(prices)
    if since is None:
        # 從最新交易日往前算，連假期間 (投信沒有新檔) 也還看得到假期前最後一晚抓的檔
        since = (datetime.strptime(latest_td, "%Y%m%d") - timedelta(days=RECENT_DAYS)).strftime("%Y%m%d")
    files = raw_files(raw_dir, since)
    newest = max((label for issuer, label, _, _ in files if issuer in SAME_DAY_ISSUERS), default="")
    if newest > latest_td:
        raise RuntimeError(f"投信已有 {newest} 的持股，但收盤價只到 {latest_td} ({price_dir})；"
                           "缺價檔時無法判定持股基準日，本次不轉")

    # (etf, 基準日) -> (寫入時間, 路徑, df)；同一基準日有多個投信檔時取最後抓到的
    chosen: dict[tuple[str, str], tuple[float, Path, pd.DataFrame]] = {}
    skipped: Counter = Counter()
    errors: list[str] = []
    for issuer, label, etf, path in files:
        try:
            df = read_raw(path)
            fit = valuation_date(df, label, prices)
        except Exception as e:                           # noqa: BLE001
            errors.append(f"{issuer}/{path.name}: {e}")
            continue
        if fit.date is None:
            skipped[fit.reason] += 1
            if verbose and fit.reason in ("poor_fit", "ambiguous"):
                print(f"[convert] 不轉 {issuer}/{path.name}: {fit.reason} "
                      f"(誤差 {fit.err:.4f}, 優勢 {fit.margin:.2f} 倍)")
            continue
        if fit.date <= CMONEY_LAST_DATE:
            skipped["cmoney_era"] += 1
            continue
        key = (etf, fit.date)
        mt = path.stat().st_mtime
        if key not in chosen or mt > chosen[key][0]:
            chosen[key] = (mt, path, df)

    written = unchanged = 0
    per_date: Counter = Counter()
    for (etf, date), (_, path, df) in sorted(chosen.items()):
        out = to_cmoney(df, etf, date)
        per_date[date] += 1
        target = out_dir / f"{date}_{etf}.csv"
        if dry_run:
            continue
        if _write(out, target):
            written += 1
            if verbose:
                print(f"[convert] {path.parent.name}/{path.name} → {target.name} ({len(out)} 列)")
        else:
            unchanged += 1

    result = {"latest_trading_day": latest_td, "n_latest": per_date.get(latest_td, 0),
              "per_date": dict(sorted(per_date.items())), "written": written,
              "unchanged": unchanged, "skipped": dict(skipped), "failed": errors}
    print(f"[convert] 完成{' (dry-run)' if dry_run else ''}: 各基準日檔數 {result['per_date']} | "
          f"新寫 {written} 未變 {unchanged} | 不轉 {result['skipped']} | 讀檔失敗 {len(errors)}")
    return result


def validate(raw_dir: Path, out_dir: Path, price_dir: Path, since: str) -> int:
    """與 CMoney 原檔比對: 日期判定是否正確、轉出的個股列數值是否一致。回傳不一致的檔數。"""
    prices = load_prices(price_dir, upto=CMONEY_LAST_DATE)
    n_ok = n_date_bad = n_value_bad = n_no_truth = n_unjudged = 0
    for issuer, label, etf, path in raw_files(raw_dir, since):
        df = read_raw(path)
        fit = valuation_date(df, label, prices)
        if fit.date is None:
            n_unjudged += 1
            continue
        cpath = out_dir / f"{fit.date}_{etf}.csv"
        if fit.date > CMONEY_LAST_DATE or not cpath.exists():
            n_no_truth += 1
            continue
        cm = pd.read_csv(cpath, dtype={"stock_id": str}, encoding="utf-8-sig")
        cm = cm[cm["stock_id"].str.match(STOCK_RE)].set_index("stock_id")
        mine = to_cmoney(df, etf, fit.date)
        mine = mine[mine["stock_id"].str.match(STOCK_RE)].drop_duplicates("stock_id").set_index("stock_id")
        common = cm.index.intersection(mine.index)
        same_ids = len(cm.index.symmetric_difference(mine.index)) == 0
        h_ok = len(common) > 0 and float((cm.loc[common, "holdings"] - mine.loc[common, "holdings"]).abs().max()) <= 0.0011
        w_ok = len(common) > 0 and float((cm.loc[common, "weight(%)"] - mine.loc[common, "weight(%)"]).abs().max()) <= 0.011
        if same_ids and h_ok and w_ok:
            n_ok += 1
        elif h_ok or w_ok:
            n_value_bad += 1
            print(f"[validate] 數值不一致 {issuer}/{path.name} vs {cpath.name}: "
                  f"代號相同={same_ids} 張數一致={h_ok} 權重一致={w_ok}")
        else:
            n_date_bad += 1
            print(f"[validate] 與判定日的 CMoney 檔不符 {issuer}/{path.name} → {fit.date}")
    print(f"[validate] 完全一致 {n_ok} | 數值不一致 {n_value_bad} | 日期疑誤 {n_date_bad} | "
          f"判定不出 {n_unjudged} | 無 CMoney 對照 {n_no_truth}")
    return n_value_bad + n_date_bad


def _heartbeat(result: dict, ok: bool):
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        from common import heartbeat
        heartbeat.write(
            "etf_holdings_convert", status="ok" if ok else "fail", exit_code=0 if ok else 1,
            message=f"{result['latest_trading_day']} 轉出 {result['n_latest']} 檔",
            stats={"latest": result["latest_trading_day"], "n_latest": result["n_latest"],
                   "written": result["written"], "skipped": result["skipped"],
                   "failed": result["failed"]})
    except Exception:                                    # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="投信原始持股 → CMoney 格式持股檔")
    ap.add_argument("--since", help=f"投信檔名日期下限 YYYYMMDD (預設最新交易日往前 {RECENT_DAYS} 天)")
    ap.add_argument("--dry-run", action="store_true", help="只印不寫")
    ap.add_argument("--require-latest", action="store_true",
                    help=f"最新交易日轉出少於 {MIN_LATEST_FILES} 檔時 exit 1")
    ap.add_argument("--validate", action="store_true", help="與 CMoney 原檔比對後結束")
    ap.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--price-dir", type=Path, default=PRICE_DIR)
    ap.add_argument("--quiet", action="store_true", help="不逐檔列印")
    args = ap.parse_args(argv)

    if args.validate:
        return 1 if validate(args.raw_dir, args.out_dir, args.price_dir, args.since or "20260901") else 0

    try:
        result = convert(args.raw_dir, args.out_dir, args.price_dir, args.since,
                         dry_run=args.dry_run, verbose=not args.quiet)
    except RuntimeError as e:
        print(f"[convert][error] {e}", file=sys.stderr)
        if not args.dry_run:
            _heartbeat({"latest_trading_day": "", "n_latest": 0, "written": 0,
                        "skipped": {}, "failed": [str(e)]}, False)
        return 1
    ok = not result["failed"]
    if args.require_latest and result["n_latest"] < MIN_LATEST_FILES:
        print(f"[convert][error] 最新交易日 {result['latest_trading_day']} 只轉出 {result['n_latest']} 檔 "
              f"(< {MIN_LATEST_FILES})：投信尚未公告、價檔缺漏或爬蟲異常", file=sys.stderr)
        ok = False
    if not args.dry_run:
        _heartbeat(result, ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
