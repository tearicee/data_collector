"""比對 ETF 最近兩個交易日的持股變化 (張數增減 / 新進 / 剔除 / 權重變化)"""
import sys
import glob
import os
import pandas as pd

DATA_DIR = "/mnt/d/etf_daily_holdings/data"


def latest_two(etf):
    files = sorted(glob.glob(os.path.join(DATA_DIR, f"*_{etf}.csv")))
    return files[-2], files[-1]


def load(path):
    df = pd.read_csv(path, encoding="utf-8-sig")
    df["stock_id"] = df["stock_id"].astype(str)
    return df


def compare(etf):
    prev_f, curr_f = latest_two(etf)
    prev_d = os.path.basename(prev_f).split("_")[0]
    curr_d = os.path.basename(curr_f).split("_")[0]
    prev, curr = load(prev_f), load(curr_f)

    m = curr.merge(prev, on="stock_id", how="outer",
                   suffixes=("_now", "_prev"), indicator=True)
    m["h_now"] = m["holdings_now"].fillna(0)
    m["h_prev"] = m["holdings_prev"].fillna(0)
    m["d_shares"] = m["h_now"] - m["h_prev"]
    m["w_now"] = m["weight(%)_now"].fillna(0)
    m["w_prev"] = m["weight(%)_prev"].fillna(0)
    m["d_weight"] = m["w_now"] - m["w_prev"]

    print(f"\n{'='*72}")
    print(f"  {etf}  持股變化   {prev_d} → {curr_d}")
    print(f"{'='*72}")

    new = m[m["_merge"] == "left_only"].sort_values("w_now", ascending=False)
    rm = m[m["_merge"] == "right_only"].sort_values("h_prev", ascending=False)

    if len(new):
        print(f"\n  ★ 新進成分股 ({len(new)})")
        print(f"  {'代號':<8}{'權重%':>8}{'張數':>14}")
        for _, r in new.iterrows():
            print(f"  {r['stock_id']:<8}{r['w_now']:>8.2f}{r['h_now']:>14,.0f}")

    if len(rm):
        print(f"\n  ✕ 剔除成分股 ({len(rm)})")
        print(f"  {'代號':<8}{'原權重%':>8}{'原張數':>14}")
        for _, r in rm.iterrows():
            print(f"  {r['stock_id']:<8}{r['w_prev']:>8.2f}{r['h_prev']:>14,.0f}")

    both = m[m["_merge"] == "both"].copy()
    both["abs_d"] = both["d_shares"].abs()
    chg = both[both["abs_d"] > 0.5].sort_values("abs_d", ascending=False)

    print(f"\n  ▲▼ 張數增減幅度最大 (前 15 大，共 {len(chg)} 檔有變動)")
    print(f"  {'代號':<8}{'權重%':>8}{'權重Δ':>9}{'今張數':>13}{'張數Δ':>13}{'增減%':>9}")
    for _, r in chg.head(15).iterrows():
        pct = (r["d_shares"] / r["h_prev"] * 100) if r["h_prev"] else float("nan")
        arrow = "▲" if r["d_shares"] > 0 else "▼"
        print(f"  {r['stock_id']:<8}{r['w_now']:>8.2f}{r['d_weight']:>+9.2f}"
              f"{r['h_now']:>13,.0f}{r['d_shares']:>+13,.0f}{pct:>+8.1f}%{arrow}")

    print(f"\n  小計：今日 {len(curr)} 檔，昨日 {len(prev)} 檔，"
          f"異動 {len(chg)} 檔，新進 {len(new)} 檔，剔除 {len(rm)} 檔")


if __name__ == "__main__":
    etfs = sys.argv[1:] or ["00878", "00919"]
    for etf in etfs:
        compare(etf)
