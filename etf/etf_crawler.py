"""
ETF 每日持股爬蟲
- 資料來源: CMoney API
- 抓取名單: MOPS 基金主檔 fund_codes.json (全母體，由 mops_fund_list.py 產生)；
            讀不到時 fallback 到內建 28 檔。
- 儲存路徑: D:/etf_daily_holdings/data/
- 排程: 每天 18:55~19:05 隨機時間執行 (cron 18:55 觸發，隨機延遲 0~600 秒)
- 每支 ETF 爬取間隔: 隨機 SLEEP_MIN~SLEEP_MAX 秒

回傳結果分為四類，避免全母體 (約 263 檔) 的告警洗版:
  ok     成功抓到當日資料
  stale  來源尚未更新 (日期不符) → 列入稍後補抓
  nodata 查無成分股資料 (新掛牌未公告 / 已下市 / CMoney 未收錄) → 記 unsupported，不告警
  failed 網路 / 解析 / 驗證錯誤 → 計入失敗，發 Discord 告警
"""

import os
import sys
import json
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
    FUND_MASTER_DIR = r"D:\etf_daily_holdings\fund_master"
else:
    # WSL 環境
    DATA_DIR = "/mnt/d/etf_daily_holdings/data"
    FUND_MASTER_DIR = "/mnt/d/etf_daily_holdings/fund_master"

FUND_CODES_JSON = os.path.join(FUND_MASTER_DIR, "fund_codes.json")
UNSUPPORTED_JSON = os.path.join(FUND_MASTER_DIR, "unsupported.json")
UNSUPPORTED_THRESHOLD = 3     # 連續無資料達此天數，於健檢摘要標示 (仍每日嘗試)

# 主檔讀不到時的保底名單 (原始追蹤 28 檔)
FALLBACK_STOCK_IDS = [
    "00982A", "00981A", "00878", "00919", "00918",
    "00940", "00929", "00713", "00939", "00881",
    "00900", "0050", "0056", "00934", "00932",
    "00923", "00915", "00936", "00733", "0057",
    "00403A", "0052", "00891", "00991A", "00913",
    "00992A", "00405A", "00400A",
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
SLEEP_MIN = 5            # 每支 ETF 間隔下限 (全母體約 263 檔，5~8s 約 25~35 分鐘)
SLEEP_MAX = 8            # 每支 ETF 間隔上限
FINAL_SWEEP = True       # 全部跑完後，對 stale/failed 的 ETF 再做補抓
SWEEP_DELAY = 3600       # 補抓前等待秒數 (1 小時)；等來源發布最新資料後再抓
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
# 抓取名單 / 無資料清單
# ============================================================
def _apply_exclusions(codes: list[str]) -> list[str]:
    """套用共用排除清單 (etf/exclusions.py)：排除商品/發行商不抓、不告警。"""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import exclusions
        kept, dropped = exclusions.filter_codes(codes)
        if dropped:
            logging.info(f"排除 {len(dropped)} 檔 (exclusions.py): {dropped}")
        return kept
    except Exception as e:                               # noqa: BLE001
        logging.warning(f"套用排除清單失敗 ({e})，維持原名單")
        return codes


def load_stock_ids() -> list[str]:
    """讀 MOPS 主檔 fund_codes.json (全母體)，套用排除清單。讀不到時退回內建 28 檔。"""
    try:
        with open(FUND_CODES_JSON, encoding="utf-8") as f:
            codes = json.load(f)
        if codes:
            logging.info(f"讀取主檔名單 {len(codes)} 檔 ({FUND_CODES_JSON})")
            return _apply_exclusions(list(codes))
        logging.warning("主檔名單為空，改用內建 fallback 名單")
    except FileNotFoundError:
        logging.warning("找不到 fund_codes.json，改用內建 fallback 名單 (請先跑 mops_fund_list.py)")
    except Exception as e:
        logging.warning(f"讀取主檔名單失敗 ({e})，改用內建 fallback 名單")
    return _apply_exclusions(list(FALLBACK_STOCK_IDS))


def load_unsupported() -> dict:
    """{代號: 連續無資料天數}。"""
    try:
        with open(UNSUPPORTED_JSON, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_unsupported(d: dict):
    try:
        os.makedirs(FUND_MASTER_DIR, exist_ok=True)
        with open(UNSUPPORTED_JSON, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=0)
    except Exception as e:
        logging.warning(f"寫入 unsupported.json 失敗: {e}")


# ============================================================
# 爬蟲核心
# ============================================================
class StaleDataError(Exception):
    """回傳資料日期不等於預期交易日 (來源尚未更新)。

    短時間內重試只會拿到同一份舊資料，故與一般網路/解析錯誤分開處理：
    不做指數退避重試，改列入稍後 (SWEEP_DELAY) 的補抓。
    """


class NoDataError(Exception):
    """來源查無此代號的成分股資料 (新掛牌未公告 / 已下市 / CMoney 未收錄)。

    可能是暫時限流造成的空回應，故仍做有限重試；重試耗盡後判定為無資料，
    記入 unsupported.json，不計入硬失敗、不發告警。
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

    - Title 缺失 → ValueError (計入 failed)
    - Data 為空 → NoDataError (可能限流，重試；耗盡判 nodata)
    - 日期不符 → StaleDataError (來源尚未更新，列入補抓)
    """
    if not res_json.get("Title") or len(res_json["Title"]) < 5:
        raise ValueError("回傳缺少 Title 欄位")
    if not res_json.get("Data"):
        raise NoDataError("回傳 Data 為空 (查無資料或暫時被限流)")

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
) -> tuple[pd.DataFrame | None, str]:
    """
    爬取單支 ETF 的持股資料，回傳 (DataFrame|None, reason)。
    reason ∈ {"ok", "stale", "nodata", "failed"}
    - ok:     成功並已存檔
    - stale:  來源尚未更新 (不短重試，列入補抓)
    - nodata: 查無資料 (有限重試後仍空)
    - failed: 網路/解析/驗證錯誤 (指數退避重試耗盡)
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
            return df, "ok"

        except StaleDataError as e:
            # 來源資料日期不符 (尚未更新)：短時間內重試也只會拿到同一份舊資料，
            # 因此不做指數退避重試，直接列入稍後 (SWEEP_DELAY) 的補抓。
            logging.warning(f"[{stock_id}] {e} → 列入稍後補抓 (不重複抓舊資料)")
            return None, "stale"

        except NoDataError as e:
            # 可能是暫時限流造成的空回應，故仍重試；耗盡後判定無資料。
            if attempt < MAX_RETRIES:
                delay = min(RETRY_BASE_DELAY * (2 ** (attempt - 1)), RETRY_MAX_DELAY)
                delay += random.uniform(0, delay * 0.3)
                logging.warning(
                    f"[{stock_id}] 第 {attempt}/{MAX_RETRIES} 次空資料: {e} "
                    f"→ {delay:.1f} 秒後重試"
                )
                time.sleep(delay)
            else:
                logging.info(f"[{stock_id}] 連續空資料，判定無資料 (nodata)")
                return None, "nodata"

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
                return None, "failed"

    return None, "failed"


def run_crawler(expected_date: str | None = None, stock_ids: list[str] | None = None):
    """執行所有 ETF 的爬蟲。

    expected_date: 預期交易日 (YYYYMMDD)，預設為今天。
    stock_ids: 覆寫抓取名單 (預設讀 MOPS 主檔)。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    setup_logging()

    if expected_date is None:
        expected_date = datetime.now().strftime("%Y%m%d")
    if stock_ids is None:
        stock_ids = load_stock_ids()

    unsupported = load_unsupported()

    logging.info("=" * 60)
    logging.info("ETF 持股爬蟲啟動")
    logging.info(f"儲存目錄: {DATA_DIR}")
    logging.info(f"ETF 數量: {len(stock_ids)}")
    logging.info(f"預期交易日: {expected_date}")
    logging.info("=" * 60)

    session = build_session()

    dfs = []
    result = {"ok": [], "stale": [], "nodata": [], "failed": []}
    for i, stock_id in enumerate(stock_ids):
        df, reason = fetch_stock_data(
            stock_id, session=session, expected_date=expected_date
        )
        if df is not None:
            dfs.append(df)
        result[reason].append(stock_id)

        # unsupported 計數: 無資料 +1，成功則清除
        if reason == "nodata":
            unsupported[stock_id] = unsupported.get(stock_id, 0) + 1
        elif reason == "ok":
            unsupported.pop(stock_id, None)

        # 最後一支不需要等待
        if i < len(stock_ids) - 1:
            sleep_time = random.uniform(SLEEP_MIN, SLEEP_MAX)
            logging.info(f"等待 {sleep_time:.1f} 秒...")
            time.sleep(sleep_time)

    # ---- 補抓: 只對 stale (來源未更新) + failed (網路錯誤) 補抓 ----
    # nodata 已確定無資料，不補抓 (下次每日執行時仍會再試，成功即自動移出 unsupported)。
    sweep_targets = result["stale"] + result["failed"]
    for rnd in range(1, SWEEP_ROUNDS + 1):
        if not (FINAL_SWEEP and sweep_targets):
            break
        logging.info("=" * 60)
        logging.info(
            f"第 {rnd}/{SWEEP_ROUNDS} 輪補抓，待補 ETF ({len(sweep_targets)}): {sweep_targets}"
        )
        logging.info(f"等待 {SWEEP_DELAY // 60} 分鐘後開始補抓 (待來源更新)...")
        time.sleep(SWEEP_DELAY)

        still = []
        for i, stock_id in enumerate(sweep_targets):
            if i > 0:
                time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))
            df, reason = fetch_stock_data(
                stock_id, session=session, expected_date=expected_date
            )
            if df is not None:
                dfs.append(df)
                # 補抓成功者從原分類移到 ok
                for k in ("stale", "failed"):
                    if stock_id in result[k]:
                        result[k].remove(stock_id)
                result["ok"].append(stock_id)
                unsupported.pop(stock_id, None)
            elif reason == "nodata":
                for k in ("stale", "failed"):
                    if stock_id in result[k]:
                        result[k].remove(stock_id)
                result["nodata"].append(stock_id)
                unsupported[stock_id] = unsupported.get(stock_id, 0) + 1
            else:
                still.append(stock_id)
        sweep_targets = still

    save_unsupported(unsupported)

    # ---- 總結 ----
    if dfs:
        combined = pd.concat(dfs, ignore_index=True)
        logging.info(
            f"爬蟲完成，成功 {len(result['ok'])}/{len(stock_ids)} 支，共 {len(combined)} 筆資料"
        )
    else:
        logging.warning("本次爬蟲未取得任何資料")

    logging.info(
        f"分類統計 — ok:{len(result['ok'])} stale:{len(result['stale'])} "
        f"nodata:{len(result['nodata'])} failed:{len(result['failed'])}"
    )
    if result["failed"]:
        logging.error(f"真失敗 (網路/解析) ETF ({len(result['failed'])}): {result['failed']}")
    if result["stale"]:
        logging.warning(f"來源未更新 ETF ({len(result['stale'])}): {result['stale']}")

    _report(result, len(stock_ids), unsupported)


def _report(result: dict, total: int, unsupported: dict):
    """寫資料層心跳；有真失敗時發 Discord 告警 (不中斷 pipeline)。"""
    # 心跳
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from common import heartbeat
        heartbeat.write(
            "etf_crawler",
            status="fail" if result["failed"] else "ok",
            exit_code=0,
            message=f"成功 {len(result['ok'])}/{total}",
            stats={
                "total": total,
                "ok": len(result["ok"]),
                "stale": len(result["stale"]),
                "nodata": len(result["nodata"]),
                "failed": result["failed"],
                "unsupported": len(unsupported),
            },
        )
    except Exception:
        pass

    # 真失敗告警
    if result["failed"]:
        try:
            from common import notify_discord
            msg = (f"CMoney 持股爬蟲有 {len(result['failed'])} 檔真失敗 "
                   f"(網路/解析)：\n{', '.join(result['failed'])}")
            notify_discord.alert("etf_crawler", msg, exit_code=None)
        except Exception:
            pass


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

    # 解析 --codes 0050,00679B (覆寫抓取名單，測試用)
    stock_ids = None
    if "--codes" in sys.argv:
        idx = sys.argv.index("--codes")
        if idx + 1 < len(sys.argv):
            stock_ids = [c.strip() for c in sys.argv[idx + 1].split(",") if c.strip()]

    # 若帶入 --now 參數則立即執行 (不隨機延遲)，方便手動測試
    if "--now" in sys.argv:
        logging.info("手動模式: 立即執行，跳過隨機延遲")
    else:
        random_delay()

    run_crawler(expected_date=expected_date, stock_ids=stock_ids)
