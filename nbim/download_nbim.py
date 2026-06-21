"""
Download all NBIM (Norway Government Pension Fund Global) holdings data.
Source: https://www.nbim.no/en/investments/all-investments/
"""

import requests
import json
import csv
import time
import os

BASE_URL = "https://www.nbim.no/api/investments/v2"
OUTPUT_DIR = "/home/tearicee/data_collector/nbim/nbim_data"
COMBINED_CSV = "/home/tearicee/data_collector/nbim/nbim_all_holdings.csv"

# Asset type mapping
ASSET_TYPE_MAP = {
    0: "Equity",
    1: "Fixed Income",
    2: "Real Estate",
    3: "Infrastructure",
}

def get_periods():
    """Fetch all available reporting periods from init.json"""
    resp = requests.get(f"{BASE_URL}/init.json", timeout=30)
    resp.raise_for_status()
    data = resp.json()
    # Structure: {"data": {"details": [...], "nav": [...], "history": {...}}}
    details = data.get("data", {}).get("details", [])
    periods = []
    for item in details:
        periods.append({
            "label": item.get("label", ""),
            "date": item.get("date", ""),
            "filename": item.get("fileName", ""),
            "interim": item.get("interim", False),
        })
    return periods


def download_period(filename):
    """Download holdings data for a specific period"""
    url = f"{BASE_URL}/{filename}"
    print(f"  Downloading {url} ...")
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.json()


def parse_holdings(period_info, raw_data):
    """Parse raw JSON into flat rows"""
    rows = []
    period_label = period_info["label"]
    period_date = period_info["date"][:10] if period_info["date"] else ""
    interim = period_info["interim"]

    entries = raw_data.get("data", [])
    if isinstance(entries, dict):
        entries = entries.get("data", [])
    for entry in entries:
        assets = entry.get("a", {})
        eq = entry.get("eq", {})
        fi = entry.get("fi", {})
        re_data = entry.get("re", {})

        row = {
            "period": period_label,
            "period_date": period_date,
            "interim": interim,
            "company_name": entry.get("n", ""),
            "country_code": entry.get("cc", ""),
            "asset_type": ASSET_TYPE_MAP.get(entry.get("at", -1), str(entry.get("at", ""))),
            "market_value_nok": assets.get("e", ""),
            "nominal_value": assets.get("n", ""),
            "ownership_pct": entry.get("o", ""),
            "sector": eq.get("s", "") or fi.get("s", ""),
            "ticker": eq.get("vid", "") or fi.get("vid", ""),
            "incorporation_country": eq.get("icc", ""),
            "voting_pct": eq.get("v", ""),
        }
        rows.append(row)
    return rows


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("Fetching available periods...")
    periods = get_periods()
    print(f"Found {len(periods)} periods:\n")
    for p in periods:
        tag = " (interim)" if p["interim"] else ""
        print(f"  {p['label']}{tag} -> {p['filename']}")

    all_rows = []

    for i, period in enumerate(periods):
        filename = period["filename"]
        label = period["label"]
        print(f"\n[{i+1}/{len(periods)}] Processing {label} ({filename})...")

        try:
            raw = download_period(filename)

            # Save raw JSON
            json_path = os.path.join(OUTPUT_DIR, filename)
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False)
            print(f"  Saved raw JSON: {json_path}")

            # Parse into rows
            rows = parse_holdings(period, raw)
            all_rows.extend(rows)
            print(f"  Parsed {len(rows)} holdings")

        except Exception as e:
            print(f"  ERROR: {e}")

        # Be polite to the server
        if i < len(periods) - 1:
            time.sleep(1)

    # Write combined CSV
    if all_rows:
        fieldnames = [
            "period", "period_date", "interim",
            "company_name", "country_code", "asset_type",
            "market_value_nok", "nominal_value", "ownership_pct",
            "sector", "ticker", "incorporation_country", "voting_pct",
        ]
        with open(COMBINED_CSV, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_rows)
        print(f"\nCombined CSV saved: {COMBINED_CSV}")
        print(f"Total rows: {len(all_rows)}")
    else:
        print("\nNo data collected!")


if __name__ == "__main__":
    main()
