# FinMind 資料收集 (Taiwan Market Tick / 分點 Data Collector)

透過 [FinMind](https://finmindtrade.com/) API 下載台股逐筆、期貨/選擇權逐筆、以及分點進出資料，
存成 parquet。含歷史回溯下載、每日自動更新、Google Drive 同步、以及避免 IP 被封鎖的保護機制。

> 需要 FinMind **SponsorPro** 等級會員 token（逐筆/分點資料權限）。

## 資料集

| 資料集 | 內容 | 端點 | 取得方式 |
|---|---|---|---|
| `TaiwanStockPriceTick` | 台股逐筆成交 | `storage_objects` | 一次拿特定日期、所有股票 (parquet) |
| `TaiwanStockTradingDailyReport` | 台股分點進出 (券商分點買賣超) | `storage_objects` | 一次拿特定日期、所有資料 (parquet) |
| `TaiwanFuturesTick` | 期貨逐筆成交 | `/api/v4/data` | 一次拿特定日期、所有商品 (JSON→parquet) |
| `TaiwanOptionTick` | 選擇權逐筆成交 | `/api/v4/data` | 一次拿特定日期、所有商品 (JSON→parquet) |

### 技術面資料集 (15 項，`download_technical.py` + `download_tick.py`)

FinMind「台股 - 技術面」分類，完整歷史回補。依取得方式分四類：

| 取得方式 (mode) | 端點 | 資料集 | 輸出 |
|---|---|---|---|
| 整表快照 snapshot | `/api/v4/data` (無日期) | `TaiwanStockInfo`、`TaiwanStockInfoWithWarrant`、`TaiwanStockInfoWithWarrantSummary`（限 sponsor）、`TaiwanStockTradingDate` | `<dataset>/<dataset>.parquet`（覆寫） |
| 通知表 range_snapshot | `/api/v4/data` (整段) | `TaiwanStockDayTradingSuspension`（限 backer/sponsor） | `<dataset>/<dataset>.parquet`（覆寫） |
| 逐日整市場 daily | `/api/v4/data` (start=end) | `TaiwanStockPrice`、`TaiwanStockPriceAdj`、`TaiwanStockDayTrading`、`TaiwanStockPriceLimit`、`TaiwanVariousIndicators5Seconds`、`TaiwanStockEvery5SecondsIndex` | `<dataset>/<年>/<dataset>_<日期>.parquet` |
| 週K / 月K weekly·monthly | `/api/v4/data` (date=週一/月初) | `TaiwanStockWeekPrice`、`TaiwanStockMonthPrice` | 同上 |
| 整日全市場 storage | `storage_objects` | `TaiwanStockKBar`（分K，2019- 起） | 同上 |

> `TaiwanStockPriceTick`（逐筆，第 9 項）與既有 tick 下載**重複**，已由 `daily_update.sh` 維護，技術面流程不重複處理。
> 各資料集已知最早可用日期記於 `download_technical.py` 的 `REGISTRY`。

資料輸出至 `D:/finmind_data/<dataset>/<年>/<dataset>_<日期>.parquet`（不進版控）。

## 腳本

| 腳本 | 用途 |
|---|---|
| `download_tick.py` | 下載 `storage_objects` 整日資料（股票 tick / 分K / 分點），`--dataset` 切換；`--blackout` 盤中暫停 |
| `download_data_tick.py` | 下載 `/api/v4/data` 期貨/選擇權逐筆（整日所有商品，轉 parquet） |
| `download_technical.py` | 下載 `/api/v4/data` 技術面 13 集（快照/逐日/週/月），`REGISTRY` 登錄各集 mode 與最早日；`--blackout` 盤中暫停 |
| `check_token.py` | 檢查 token 是否仍為有效 SponsorPro（供每日更新判斷是否續抓） |
| `daily_update.sh` | 每日更新最近 7 天股票 tick / 分點 / 期貨 / 選擇權，並同步股票 tick 至 Google Drive |
| `daily_technical.sh` | 每日更新技術面 15 集（最近 40 天視窗 + 整表快照刷新；夜間 cron） |
| `backup_to_gdrive.sh` | 用 rclone 將股票 tick 同步到 Google Drive |
| `backfill_progress.py` | 查各資料集下載進度（%、涵蓋日期、大小、執行中程序） |
| `start_finmind_backfill.sh` / `stop_finmind_backfill.sh` | 啟動/停止大型歷史回溯下載（期貨/選擇權/分點並行） |
| `start_technical_backfill.sh` / `stop_technical_backfill.sh` | 啟動/停止技術面完整歷史回補（內建 08:00–14:30 盤中自動暫停） |

## 用法

```bash
python3 -m venv venv && ./venv/bin/pip install requests pandas pyarrow python-dotenv
cp .env.example .env   # 填入 finmind_api_key

# 單一日期 / 區間（reverse 從近期往回）
./venv/bin/python download_tick.py --dataset TaiwanStockPriceTick --start 2024-01-01 --end 2024-12-31 --reverse
./venv/bin/python download_data_tick.py --datasets TaiwanFuturesTick,TaiwanOptionTick --start 2019-01-01 --reverse

# 查進度
./venv/bin/python backfill_progress.py
```

## 防 IP 封鎖

FinMind 在短時間累積大量 4xx（無效 token / 參數錯誤 / 超額）會自動封鎖 IP。本專案：

- **連續失敗熔斷**：連續 8 次 fail 立即中止整個任務
- **每次請求最小間隔**：避免短時間爆量請求
- **401/402/403 立即乾淨中止**：token 失效/降級不續打
- SponsorPro 每小時 20000 次上限，慢速 backfill 遠低於上限

## 盤中網路讓道 (08:00–14:30)

技術面下載器 (`download_technical.py` / 帶 `--blackout` 的 `download_tick.py`) 在 **08:00–14:30**
（台股盤前+盤中）期間**自動暫停**，不發出任何網路請求，每 60 秒檢查一次，時段結束自動續跑。
回補腳本 `start_technical_backfill.sh` 預設帶 `--blackout 08:00-14:30`，故大型回補可整天掛著、
盤中自動讓出頻寬，無需 stop/start cron。每日更新排在夜間，本就不在此時段。

## 資料時段備註

期貨/選擇權含**日盤 08:45–13:45** 與**夜盤 15:00–隔日 05:00**；夜盤跨午夜，故某日檔案中
凌晨 00:00–05:00 屬前一交易日夜盤的延續。
