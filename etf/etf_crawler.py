"""
ETF 每日持股爬蟲
- 資料來源: CMoney API
- 儲存路徑: D:/etf_daily_holdings/data/
- 排程: 每天 18:55~19:05 隨機時間執行 (cron 18:55 觸發，隨機延遲 0~600 秒)
- 每支 ETF 爬取間隔: 隨機 9~13 秒
"""

import os
import sys
import random
import time
import logging
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import pandas as pd

# ============================================================
# 設定
# ============================================================
# 根據作業系統自動判斷儲存路徑 (WSL 用 /mnt/d, Windows 用 D:/)
if sys.platform == "win32":
    DATA_DIR = r"D:\etf_daily_holdings\data"
else:
    # WSL 環境
    DATA_DIR = "/mnt/d/etf_daily_holdings/data"

STOCK_IDS = [
    "00982A", "00981A", "00878", "00919", "00918",
    "00940", "00929", "00713", "00939", "00881",
    "00900", "0050", "0056", "00934", "00932",
    "00923", "00915", "00936", "00733", "0057",
    "00403A", "0052", "00891", "00991A", "00913",
]

API_URL = (
    "https://www.cmoney.tw/api/cm/MobileService/ashx/GetDtnoData.ashx"
    "?action=getdtnodata&DtNo=59449513"
    "&ParamStr=AssignID%3D{}%3BMTPeriod%3D0%3BDTMode%3D0%3BDTRange%3D1%3BDTOrder%3D1%3BMajorTable%3DM722%3B"
    "&FilterNo=0"
)

# ============================================================
# 重試設定
# ============================================================
MAX_RETRIES = 4          # 單支 ETF 的最大嘗試次數 (含第一次)
RETRY_BASE_DELAY = 5     # 重試退避基準秒數 (指數退避: 5, 10, 20, ...)
RETRY_MAX_DELAY = 60     # 單次重試最長等待秒數
REQUEST_TIMEOUT = (10, 30)  # (連線逾時, 讀取逾時) 秒
FINAL_SWEEP = True       # 全部跑完後，對仍失敗/來源未更新的 ETF 再做補抓
SWEEP_DELAY = 3600       # 補抓前等待秒數 (1 小時)；等來源發布最新資料後再抓，避免重複抓到舊資料
SWEEP_ROUNDS = 1         # 補抓輪數 (每輪間隔 SWEEP_DELAY)

# 隨機 User-Agent 池，降低被偵測的風險
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

# ============================================================
# 日誌設定
# ============================================================
LOG_DIR = os.path.join(DATA_DIR, "logs")


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = os.path.join(LOG_DIR, f"crawler_{datetime.now():%Y%m%d}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


# ============================================================
# 爬蟲核心
# ============================================================
class StaleDataError(Exception):
    """回傳資料日期不等於預期交易日 (來源尚未更新)。

    短時間內重試只會拿到同一份舊資料，故與一般網路/解析錯誤分開處理：
    不做指數退避重試，改列入稍後 (SWEEP_DELAY) 的補抓。
    """


def build_session() -> requests.Session:
    """
    建立帶有傳輸層自動重試的 Session。
    - 針對連線/讀取錯誤、429 (限流)、5xx 伺服器錯誤自動重試
    - backoff_factor 讓每次重試間隔指數成長 (1, 2, 4 秒...)
    """
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _parse_response(
    stock_id: str, res_json: dict, expected_date: str | None = None
) -> pd.DataFrame:
    """將 API 回傳的 JSON 解析為 DataFrame；資料不合法時拋出例外以觸發重試。

    若 expected_date 有指定且回傳資料日期不符 (來源尚未更新)，拋出
    StaleDataError，讓呼叫端列入稍後補抓，避免用舊資料覆蓋當日檔。
    """
    if not res_json.get("Title") or len(res_json["Title"]) < 5:
        raise ValueError("回傳缺少 Title 欄位")
    if not res_json.get("Data"):
        raise ValueError("回傳 Data 為空 (可能尚未更新或被限流)")

    data_title = [
        res_json["Title"][0],
        res_json["Title"][1],
        res_json["Title"][3],
        res_json["Title"][4],
    ]
    data = [[d[0], d[1], d[3], d[4]] for d in res_json["Data"]]
    df = pd.DataFrame(data, columns=data_title)
    df.columns = ["date", "stock_id", "weight(%)", "holdings"]
    df["holdings"] = df["holdings"].str.replace(",", "").astype("int64") / 1000
    df["etf"] = stock_id

    got_date = str(df["date"].iloc[0])
    if expected_date and got_date != expected_date:
        raise StaleDataError(
            f"回傳日期 {got_date} ≠ 預期交易日 {expected_date} (來源尚未更新)"
        )
    return df


def fetch_stock_data(
    stock_id: str,
    session: requests.Session | None = None,
    expected_date: str | None = None,
) -> pd.DataFrame | None:
    """
    爬取單支 ETF 的持股資料，內建應用層重試。
    - 每次嘗試會輪替 User-Agent
    - 失敗 (網路/解析/驗證) 時以指數退避 + 隨機抖動後重試
    - 來源尚未更新 (日期不符) 時不做短重試，直接回傳 None 交給稍後補抓
    - 全部嘗試皆失敗才回傳 None
    """
    url = API_URL.format(stock_id)
    session = session or build_session()

    for attempt in range(1, MAX_RETRIES + 1):
        headers = {"User-Agent": random.choice(USER_AGENTS)}
        try:
            res = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            res.raise_for_status()
            res_json = res.json()
            df = _parse_response(stock_id, res_json, expected_date)

            d = df["date"].iloc[0]
            file_path = os.path.join(DATA_DIR, f"{d}_{stock_id}.csv")
            df.to_csv(file_path, encoding="utf-8_sig", index=False)
            logging.info(
                f"[{stock_id}] 日期={d}, 筆數={len(df)}, "
                f"第 {attempt}/{MAX_RETRIES} 次成功, 儲存至 {file_path}"
            )
            return df

        except StaleDataError as e:
            # 來源資料日期不符 (尚未更新)：短時間內重試也只會拿到同一份舊資料，
            # 因此不做指數退避重試，直接列入稍後 (SWEEP_DELAY) 的補抓。
            logging.warning(f"[{stock_id}] {e} → 列入稍後補抓 (不重複抓舊資料)")
            return None

        except Exception as e:
            if attempt < MAX_RETRIES:
                # 指數退避 + 隨機抖動，避免同時重試造成尖峰
                delay = min(
                    RETRY_BASE_DELAY * (2 ** (attempt - 1)), RETRY_MAX_DELAY
                )
                delay += random.uniform(0, delay * 0.3)
                logging.warning(
                    f"[{stock_id}] 第 {attempt}/{MAX_RETRIES} 次失敗: {e} "
                    f"→ {delay:.1f} 秒後重試"
                )
                time.sleep(delay)
            else:
                logging.error(
                    f"[{stock_id}] 已重試 {MAX_RETRIES} 次仍失敗: {e}"
                )

    return None


def run_crawler(expected_date: str | None = None):
    """執行所有 ETF 的爬蟲。

    expected_date: 預期交易日 (YYYYMMDD)，預設為今天。回傳資料日期不符者
    視為來源尚未更新，列入稍後補抓，避免用舊資料覆蓋當日檔。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    setup_logging()

    if expected_date is None:
        expected_date = datetime.now().strftime("%Y%m%d")

    logging.info("=" * 60)
    logging.info("ETF 持股爬蟲啟動")
    logging.info(f"儲存目錄: {DATA_DIR}")
    logging.info(f"ETF 數量: {len(STOCK_IDS)}")
    logging.info(f"預期交易日: {expected_date}")
    logging.info("=" * 60)

    session = build_session()

    dfs = []
    failed = []
    for i, stock_id in enumerate(STOCK_IDS):
        df = fetch_stock_data(
            stock_id, session=session, expected_date=expected_date
        )
        if df is not None:
            dfs.append(df)
        else:
            failed.append(stock_id)

        # 最後一支不需要等待
        if i < len(STOCK_IDS) - 1:
            sleep_time = random.uniform(9, 13)
            logging.info(f"等待 {sleep_time:.1f} 秒...")
            time.sleep(sleep_time)

    # ---- 補抓: 對仍失敗/來源未更新的 ETF，每隔 SWEEP_DELAY (約一小時) 再補抓 ----
    # 待補多為「來源尚未更新」，故先等待約一小時讓來源端發布最新資料後再抓，
    # 避免短時間內重複抓到同一份舊資料。
    for rnd in range(1, SWEEP_ROUNDS + 1):
        if not (FINAL_SWEEP and failed):
            break
        logging.info("=" * 60)
        logging.info(
            f"第 {rnd}/{SWEEP_ROUNDS} 輪補抓，待補 ETF ({len(failed)}): {failed}"
        )
        logging.info(f"等待 {SWEEP_DELAY // 60} 分鐘後開始補抓 (待來源更新)...")
        time.sleep(SWEEP_DELAY)

        still_failed = []
        for i, stock_id in enumerate(failed):
            if i > 0:
                # 補抓內各 ETF 間隔，避免短時連續請求
                time.sleep(random.uniform(9, 13))
            df = fetch_stock_data(
                stock_id, session=session, expected_date=expected_date
            )
            if df is not None:
                dfs.append(df)
            else:
                still_failed.append(stock_id)
        failed = still_failed

    if dfs:
        combined = pd.concat(dfs, ignore_index=True)
        logging.info(
            f"爬蟲完成，成功 {len(dfs)}/{len(STOCK_IDS)} 支，共 {len(combined)} 筆資料"
        )
    else:
        logging.warning("本次爬蟲未取得任何資料")

    if failed:
        logging.error(f"最終仍失敗/來源未更新的 ETF ({len(failed)}): {failed}")


# ============================================================
# 隨機延遲 (cron 18:55 觸發，隨機延遲 0~600 秒，落在 18:55~19:05)
# ============================================================
def random_delay():
    """在 0~600 秒 (0~10 分鐘) 內隨機等待，使每天的爬蟲啟動時間不固定"""
    delay = random.randint(0, 600)
    minutes, seconds = divmod(delay, 60)
    logging.info(f"隨機延遲 {minutes} 分 {seconds} 秒後開始爬蟲...")
    time.sleep(delay)


# ============================================================
# 主程式
# ============================================================
if __name__ == "__main__":
    setup_logging()

    # 解析 --date YYYYMMDD (覆寫預期交易日，方便手動補抓指定日)
    expected_date = None
    if "--date" in sys.argv:
        idx = sys.argv.index("--date")
        if idx + 1 < len(sys.argv):
            expected_date = sys.argv[idx + 1]

    # 若帶入 --now 參數則立即執行 (不隨機延遲)，方便手動測試
    if "--now" in sys.argv:
        logging.info("手動模式: 立即執行，跳過隨機延遲")
    else:
        random_delay()

    run_crawler(expected_date=expected_date)
