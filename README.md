# data_collector — 金融資料收集

收集三大類金融資料的腳本集合。三個功能各自獨立資料夾，**共用** `data_collector/.venv` 一個虛擬環境。

```
data_collector/
├── .venv/        ← 唯一共用虛擬環境 (requests / pandas / pyarrow / python-dotenv / openpyxl)
├── nbim/         ← 挪威主權基金 NBIM 全球持股
├── taifex/       ← 期交所 TAIFEX 每日期貨/選擇權成交
├── etf/          ← ETF 每日持股爬蟲 (CMoney)
└── finmind/      ← FinMind 台股逐筆 / 期貨選擇權逐筆 / 分點 (獨立 git repo)
```

> 所有腳本一律用 `/home/tearicee/data_collector/.venv/bin/python` 執行。

---

## 📁 nbim/ — 挪威主權基金持股

挪威政府全球退休金基金 (NBIM) 歷年全球持股，來源 `nbim.no/api`。一次性歷史下載，**無排程**。

| 檔案 | 用途 |
|---|---|
| `download_nbim.py` | 下載 NBIM 1998~至今全球持股，輸出 `nbim_data/*.json` 與合併 `nbim_all_holdings.csv` |
| `build_mapping.py` / `build_mapping_v2.py` / `build_mapping_final.py` | 將 NBIM 英文公司名對照台股代號/中文名 (逐版演進，final 為最終版) |
| `nbim_data/` | 各年度原始 JSON (1998~2025，含年中期中檔) |
| `nbim_all_holdings.csv` | 全球所有持股合併表 (約 24 萬列) |
| `nbim_tw_holdings.csv` | 篩出的台股持股 |
| `nbim_tw_mapping.csv` | 公司名 ↔ 台股代號對照表 (最終) |
| `nbim_tw_matched_preliminary.csv` / `nbim_tw_unmatched.txt` | 對照過程中間檔 (已配對 / 未配對) |
| `stock_info.xlsx` | 台股代號/名稱基礎表，供名稱對照使用 |

資料存於本資料夾 (非 D 槽)，**不做雲端同步**。

---

## 📁 taifex/ — 期交所每日成交

期交所每日期貨 + 選擇權成交資料 (CSV)。**每天 06:00 自動下載 + 同步 Google Drive**。

| 檔案 | 用途 |
|---|---|
| `download_taifex_daily.py` | 下載前一交易日期貨 (`Daily_*.csv`) 與選擇權 (`OptionsDaily_*.csv`)，遇假日自動往前回溯 |
| `run_taifex_daily.sh` | cron 啟動腳本：下載 → 呼叫備份同步 (stdout 導 /dev/null 避免 log 重複) |
| `backup_taifex_to_gdrive.sh` | rclone 將 `/mnt/d/Taifex/` 同步到 `gdrive:Taifex/` (增量，不刪雲端) |
| `taifex_download.log` / `taifex_backup.log` | 下載 / 備份日誌 |

**資料存放**：`D:/Taifex/futures/` (期貨)、`D:/Taifex/options/` (選擇權) → 同步至 `gdrive:Taifex/`。

---

## 📁 etf/ — ETF 每日持股爬蟲

從 CMoney API 爬取 25 支熱門 ETF 的每日持股明細。**每天 18:55 自動執行 (隨機延遲 0~10 分) + 同步 Google Drive**。

| 檔案 | 用途 |
|---|---|
| `etf_crawler.py` | 爬取 25 支 ETF 持股 (權重/張數)，每支間隔隨機 9~13 秒、含重試與補抓；`--now` 可跳過隨機延遲手動測試 |
| `run_crawler.sh` | cron 啟動腳本：隨機延遲 → 爬蟲 → 呼叫備份同步 |
| `backup_to_gdrive.sh` | rclone 將 `/mnt/d/etf_daily_holdings/data/` 同步到 `gdrive:etf_daily_holdings/` |
| `ETF持股爬蟲.ipynb` | 開發用 notebook (原型參考) |
| `etf2.zip` | 早期封存 (參考用) |

**資料存放**：`D:/etf_daily_holdings/data/`（每支 ETF 每日一檔 `<日期>_<代號>.csv`）→ 同步至 `gdrive:etf_daily_holdings/`。

---

## 📁 finmind/ — FinMind 台股資料 (獨立 git repo)

透過 FinMind API 下載逐筆/分點資料 (需 SponsorPro 等級 token，存於 `finmind/.env`，已 gitignore)。
詳見 `finmind/README.md`。

| 檔案 | 用途 |
|---|---|
| `download_tick.py` | 下載 `storage_objects` 整日資料 (股票 tick / 分點)，`--dataset` 切換 |
| `download_data_tick.py` | 下載 `/api/v4/data` 期貨/選擇權逐筆 (整日所有商品，JSON→parquet) |
| `check_token.py` | 檢查 token 是否仍為有效 SponsorPro (供每日更新判斷) |
| `daily_update.sh` | **每天 20:00** 更新最近 7 天股票 tick + 同步 Google Drive |
| `backup_to_gdrive.sh` | rclone 將股票 tick 同步到 `gdrive:finmind_data/` |
| `backfill_progress.py` | 查各 FinMind 資料集下載進度 (%、涵蓋日期、大小、執行中程序) |
| `start_finmind_backfill.sh` / `stop_finmind_backfill.sh` | 大型歷史回溯下載的啟動/停止 (期貨/選擇權/分點並行) |
| `.env` | FinMind API token (**機密，不進版控**) |
| `*.log` / `backfill_control.log` | 各下載與控制日誌 |

**資料存放** `D:/finmind_data/<dataset>/<年>/`：

| 資料集 | 內容 | 每日更新 | 雲端同步 |
|---|---|---|---|
| `TaiwanStockPriceTick` | 台股逐筆成交 | ✅ 20:00 | ✅ gdrive |
| `TaiwanStockTradingDailyReport` | 台股分點進出 | ❌ (歷史一次性) | ❌ |
| `TaiwanFuturesTick` | 期貨逐筆成交 | ❌ (歷史一次性) | ❌ |
| `TaiwanOptionTick` | 選擇權逐筆成交 | ❌ (歷史一次性) | ❌ |

逐筆資料時段：日盤 08:45–13:45、夜盤 15:00–隔日 05:00 (夜盤跨午夜)。

---

## ⏰ 自動排程 (crontab)

| 時間 | 工作 |
|---|---|
| 06:00 每天 | `taifex/run_taifex_daily.sh` — 期交所期貨+選擇權下載 + 同步雲端 |
| 18:55 週一~五 | `etf/run_crawler.sh` — ETF 持股爬蟲 (隨機延遲 0~10 分) + 同步雲端 |
| 20:00 每天 | `finmind/daily_update.sh` — FinMind 股票 tick 每日更新 + 同步雲端 |

> 其餘 crontab 項目 (tick_alert、Dashboard) 屬其他專案，不在本資料夾範圍。

## ☁️ Google Drive 同步範圍

會上傳雲端 (透過 rclone `gdrive:` remote)：**期交所 Taifex**、**ETF 持股** (`gdrive:etf_daily_holdings`)、**FinMind 股票 tick** (`gdrive:finmind_data`)。
NBIM、FinMind 分點/期貨/選擇權逐筆只存本機 D 槽。

## 🔧 共用環境

```bash
# 套件已安裝於 data_collector/.venv
/home/tearicee/data_collector/.venv/bin/python <script>
```
