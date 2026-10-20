#!/usr/bin/env python3
"""GPU 雲價格收集：抓 Nebius／Lambda／RunPod／CoreWeave 官網價格頁，解析成長表存檔。

輸出 (D:/gpu_cloud_prices)：
  prices.parquet            全部快照的長表 (snapshot_date + sources.COLUMNS)，同一天重跑會覆蓋當天
  latest.csv                最新一次快照 (給人看／Excel)
  changes.csv               每次快照相對前一次的變動／新增／消失 (有變動才寫；看調價歷史用這個)
  source/<日期>/<家>.html    當次抓到的原始頁面 (解析器壞了可以重解)
心跳 gpu_prices；任何一家解析出的列數少於 sources.MIN_ROWS 視為頁面改版，exit 1 讓排程告警。

用法：
  python gpu_prices/collect.py                 # 抓四家並存檔
  python gpu_prices/collect.py --compare       # 存檔後印各家隨選價對照表
  python gpu_prices/collect.py --from-dir DIR  # 用已存的 html 重解 (不連網)
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sources  # noqa: E402

if sys.platform == "win32":
    ROOT = Path(r"D:\gpu_cloud_prices")
else:
    ROOT = Path("/mnt/d/gpu_cloud_prices")

KEY = ["snapshot_date", "provider", "sku", "tier", "region", "gpus_per_instance", "commitment"]


def collect(root: Path = ROOT, day: dt.date | None = None, from_dir: Path | None = None,
            providers=tuple(sources.URLS)) -> tuple[pd.DataFrame, dict]:
    day = day or dt.date.today()
    stats = {"rows": {}, "failed": []}
    frames = []
    for p in providers:
        try:
            if from_dir:
                page = (from_dir / f"{p}.html").read_text(encoding="utf-8")
            else:
                page = sources.fetch(p)
                src = root / "source" / day.isoformat()
                src.mkdir(parents=True, exist_ok=True)
                (src / f"{p}.html").write_text(page, encoding="utf-8")
            rows = sources.PARSERS[p](page)
        except Exception as e:                           # noqa: BLE001
            print(f"[gpu_prices][error] {p}: {type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}",
                  file=sys.stderr)
            stats["failed"].append(p)
            continue
        stats["rows"][p] = len(rows)
        if len(rows) < sources.MIN_ROWS[p]:
            print(f"[gpu_prices][error] {p}: 只解析出 {len(rows)} 列 (< {sources.MIN_ROWS[p]})，頁面可能改版",
                  file=sys.stderr)
            stats["failed"].append(p)
            continue
        frames.append(pd.DataFrame(rows, columns=sources.COLUMNS))
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=sources.COLUMNS)
    df.insert(0, "snapshot_date", day.isoformat())
    for c in ("commitment", "region", "variant", "note", "storage"):
        df[c] = df[c].fillna("").astype(str)
    return df, stats


def save(df: pd.DataFrame, root: Path = ROOT) -> int:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "prices.parquet"
    if path.exists():
        old = pd.read_parquet(path)
        # 同一天、同一家重跑：以新的為準 (整家替換，才不會留下已消失的 SKU)
        drop = set(zip(df["snapshot_date"], df["provider"]))
        old = old[~pd.Series(list(zip(old["snapshot_date"], old["provider"]))).isin(drop).to_numpy()]
        df = pd.concat([old, df], ignore_index=True)
    df = df.sort_values(["snapshot_date", "provider", "gpu_model", "tier"], ignore_index=True)
    df.to_parquet(path, index=False)
    latest = df[df["snapshot_date"] == df["snapshot_date"].max()]
    latest.to_csv(root / "latest.csv", index=False, encoding="utf-8-sig")
    return len(df)


def compare(df: pd.DataFrame, tier: str = "on_demand") -> pd.DataFrame:
    """各家「每 GPU-小時」對照：同型號取該家最便宜的一列 (CoreWeave 以整台價換算)。"""
    sub = df[(df["tier"] == tier) & df["price_gpu_hour_usd"].notna()].copy()
    sub["gpu"] = (sub["gpu_model"] + " " + sub["variant"].fillna("")).str.strip()   # H100 SXM／PCIe 分開比
    piv = sub.pivot_table(index="gpu", columns="provider", values="price_gpu_hour_usd", aggfunc="min")
    order = ["GB200 NVL72", "B300", "B200", "H200", "H100 SXM", "H100 NVL", "H100 PCIe", "GH200", "A100 80GB SXM",
             "A100 80GB PCIe", "A100 40GB SXM", "A100 40GB PCIe", "RTX PRO 6000", "L40S", "L40", "RTX 6000 Ada",
             "RTX A6000"]
    piv = piv.reindex([g for g in order if g in piv.index] + sorted(set(piv.index) - set(order)))
    return piv.dropna(how="all")


PRICE_COLS = ["price_gpu_hour_usd", "price_instance_hour_usd"]


def changes(root: Path = ROOT, day: str | None = None) -> pd.DataFrame:
    """最新快照 vs 前一次快照：價格變動／新增／消失的列。四家官網都是牌價，變動是離散事件
    (Nebius 2026-10-01、10-08 各調一次；RunPod 頁面標 10-08 更新)，每天抓一次就能抓到確切的變動日。
    結果追加到 changes.csv (變動才寫)，看歷史變動不用自己 diff parquet。"""
    path = root / "prices.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    days = sorted(df["snapshot_date"].unique())
    day = day or days[-1]
    prev = [d for d in days if d < day]
    if not prev:
        return pd.DataFrame()
    key = ["provider", "sku", "tier", "region", "gpus_per_instance", "commitment"]
    cur = df[df["snapshot_date"] == day].set_index(key)
    old = df[df["snapshot_date"] == prev[-1]].set_index(key)
    rows = []
    for idx in cur.index.union(old.index):
        a = old.loc[idx] if idx in old.index else None
        b = cur.loc[idx] if idx in cur.index else None
        if a is None or b is None:
            kind = "新增" if a is None else "消失"
            p0, p1 = (None, b["price_gpu_hour_usd"]) if a is None else (a["price_gpu_hour_usd"], None)
        else:
            if all(pd.isna(a[c]) and pd.isna(b[c]) or a[c] == b[c] for c in PRICE_COLS):
                continue
            kind, p0, p1 = "變動", a["price_gpu_hour_usd"], b["price_gpu_hour_usd"]
        rows.append({"snapshot_date": day, "prev_date": prev[-1], "kind": kind, **dict(zip(key, idx)),
                     "gpu_model": (b if b is not None else a)["gpu_model"], "price_before": p0, "price_after": p1})
    out = pd.DataFrame(rows)
    if not out.empty:
        log = root / "changes.csv"
        if log.exists():
            seen = pd.read_csv(log, dtype=str)
            if (seen["snapshot_date"] == day).any():                 # 同一天重跑：先去掉舊的
                seen = seen[seen["snapshot_date"] != day]
            out = pd.concat([seen, out.astype(str)], ignore_index=True)
        out.to_csv(log, index=False, encoding="utf-8-sig")
        out = pd.DataFrame(rows)
    return out


def _heartbeat(stats: dict, n_total: int, ok: bool, n_changes: int) -> None:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from common import heartbeat
        heartbeat.write("gpu_prices", status="ok" if ok else "fail", exit_code=0 if ok else 1,
                        message=f"四家共 {sum(stats['rows'].values())} 列，較前次變動 {n_changes} 列", cadence="daily",
                        stats={"rows": stats["rows"], "failed": stats["failed"], "total_rows": n_total,
                               "changes": n_changes})
    except Exception:                                    # noqa: BLE001
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="收集 GPU 雲官網價格")
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--date", type=dt.date.fromisoformat, default=None, help="快照日期 (預設今天)")
    ap.add_argument("--from-dir", type=Path, default=None, help="用已存的 html 重解，不連網")
    ap.add_argument("--providers", default=",".join(sources.URLS), help="逗號分隔")
    ap.add_argument("--compare", action="store_true", help="印隨選價對照表")
    ap.add_argument("--dry-run", action="store_true", help="只解析不存檔")
    args = ap.parse_args(argv)

    df, stats = collect(args.root, args.date, args.from_dir, tuple(p for p in args.providers.split(",") if p))
    n_total, diff = len(df), pd.DataFrame()
    if not args.dry_run and not df.empty:
        n_total = save(df, args.root)
        diff = changes(args.root, df["snapshot_date"].iloc[0])
    ok = not stats["failed"]
    print(f"[gpu_prices] 完成: 各家列數 {stats['rows']} | 失敗 {stats['failed']} | 資料集共 {n_total} 列 | "
          f"較前次快照變動 {len(diff)} 列")
    if not diff.empty:
        with pd.option_context("display.width", 200, "display.max_columns", 20):
            print(diff[["kind", "provider", "sku", "tier", "region", "price_before", "price_after"]].to_string(index=False))
    if args.compare and not df.empty:
        with pd.option_context("display.width", 200, "display.max_columns", 20):
            print(compare(df).to_string(float_format=lambda v: f"{v:.2f}"))
    if not args.dry_run and not args.from_dir:
        _heartbeat(stats, n_total, ok, len(diff))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
