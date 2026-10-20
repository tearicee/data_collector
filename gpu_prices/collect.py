#!/usr/bin/env python3
"""GPU 雲價格收集：抓 Nebius／Lambda／RunPod／CoreWeave 官網價格頁，解析成長表存檔。

輸出 (D:/gpu_cloud_prices)：
  prices.parquet            全部快照的長表 (snapshot_date + sources.COLUMNS)，同一天重跑會覆蓋當天
  latest.csv                最新一次快照 (給人看／Excel)
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


def _heartbeat(stats: dict, n_total: int, ok: bool) -> None:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from common import heartbeat
        heartbeat.write("gpu_prices", status="ok" if ok else "fail", exit_code=0 if ok else 1,
                        message=f"四家共 {sum(stats['rows'].values())} 列", cadence="weekly",
                        stats={"rows": stats["rows"], "failed": stats["failed"], "total_rows": n_total})
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
    n_total = len(df)
    if not args.dry_run and not df.empty:
        n_total = save(df, args.root)
    ok = not stats["failed"]
    print(f"[gpu_prices] 完成: 各家列數 {stats['rows']} | 失敗 {stats['failed']} | 資料集共 {n_total} 列")
    if args.compare and not df.empty:
        with pd.option_context("display.width", 200, "display.max_columns", 20):
            print(compare(df).to_string(float_format=lambda v: f"{v:.2f}"))
    if not args.dry_run and not args.from_dir:
        _heartbeat(stats, n_total, ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
