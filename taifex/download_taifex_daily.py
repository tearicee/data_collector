import logging
import os
import sys
import zipfile
from datetime import date, datetime, timedelta

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_RETRIES = 15

# 資料存放於 D 槽 (空間大，因應未來資料量擴大)；log 仍留在 BASE_DIR
TAIFEX_DATA_ROOT = "/mnt/d/Taifex"

PRODUCTS = {
    "futures": {
        "url": "https://www.taifex.com.tw/file/taifex/Dailydownload/DailydownloadCSV",
        "prefix": "Daily",
        "save_dir": os.path.join(TAIFEX_DATA_ROOT, "futures"),
    },
    "options": {
        "url": "https://www.taifex.com.tw/file/taifex/Dailydownload/OptionsDailydownloadCSV",
        "prefix": "OptionsDaily",
        "save_dir": os.path.join(TAIFEX_DATA_ROOT, "options"),
    },
}

log = logging.getLogger("taifex")


def setup_logging() -> None:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    log.addHandler(console)

    log_path = os.path.join(BASE_DIR, "taifex_download.log")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)


def prev_weekday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def download_one(product: dict, target_date: date) -> bool:
    save_dir = product["save_dir"]
    os.makedirs(save_dir, exist_ok=True)

    filename = f"{product['prefix']}_{target_date.strftime('%Y_%m_%d')}.zip"
    url = f"{product['url']}/{filename}"
    zip_path = os.path.join(save_dir, filename)

    resp = requests.get(url, timeout=60)

    if "application/zip" not in resp.headers.get("Content-Type", ""):
        return False

    with open(zip_path, "wb") as f:
        f.write(resp.content)

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(save_dir)
        csv_names = zf.namelist()

    os.remove(zip_path)

    size_mb = len(resp.content) / 1024 / 1024
    log.info("OK %s (%.1f MB) -> %s", filename, size_mb, csv_names)
    return True


def download_prev_trading_day() -> None:
    candidate = prev_weekday(date.today())

    for _ in range(MAX_RETRIES):
        log.info("Trying %s ...", candidate)
        if download_one(PRODUCTS["futures"], candidate):
            download_one(PRODUCTS["options"], candidate)
            log.info("Done. Trading day: %s", candidate)
            return
        log.info("No data for %s (market closed), going back", candidate)
        candidate = prev_weekday(candidate)

    log.warning("Exhausted %d retries. No trading day data found.", MAX_RETRIES)


def download_date(target_date: date) -> None:
    for name, product in PRODUCTS.items():
        if not download_one(product, target_date):
            log.warning("[%s] No data for %s (market closed)", name, target_date)


if __name__ == "__main__":
    setup_logging()

    if len(sys.argv) > 1:
        target = date.fromisoformat(sys.argv[1])
        download_date(target)
    else:
        download_prev_trading_day()
