# data_collector — 金融資料收集

收集三大類金融資料的腳本集合。三個功能各自獨立資料夾，**共用** `data_collector/.venv` 一個虛擬環境。

```
data_collector/
├── .venv/        ← 唯一共用虛擬環境 (requests / pandas / pyarrow / python-dotenv / openpyxl)
├── nbim/         ← 挪威主權基金 NBIM 全球持股
├── taifex/       ← 期交所 TAIFEX 每日期貨/選擇權成交
├── etf/          ← ETF 每日持股 (投信官網原始持股 → CMoney 格式)
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

從各投信官網抓 ETF 每日持股，再轉成原本 CMoney 爬蟲的檔案格式供下游使用。**每天 18:55 自動執行 (約 20:40 再跑第二輪補晚公告者) + 同步 Google Drive**。

> 2026-10 起 CMoney 的 API 改為需要驗證 (回 `Auth Failed`)，`etf_crawler.py` 已不在排程內。

| 檔案 | 用途 |
|---|---|
| `raw_holdings/run_raw.py` | 各投信官網原始持股 (一家一個 adapter)，存 `D:/etf_daily_holdings_raw/<發行商>/` |
| `raw_holdings/to_cmoney.py` | 把投信原始持股轉成 CMoney 格式寫到 `D:/etf_daily_holdings/data/`；持股基準日由內容判定 (股數×收盤價重算權重)，不採用投信檔名日期；`--validate` 可與 CMoney 原檔比對 |
| `etf_crawler.py` | (停用) CMoney 持股爬蟲，保留供參考 |
| `run_crawler.sh` | cron 啟動腳本：基金主檔 → 投信持股 → 轉檔 (兩輪) → 備份同步 |
| `backup_to_gdrive.sh` | rclone 將 `/mnt/d/etf_daily_holdings/data/` 同步到 `gdrive:etf_daily_holdings/` |
| `ETF持股爬蟲.ipynb` | 開發用 notebook (原型參考) |
| `etf2.zip` | 早期封存 (參考用) |

**資料存放**：`D:/etf_daily_holdings/data/`（每支 ETF 每日一檔 `<持股基準日>_<代號>.csv`，欄位 `date, stock_id, weight(%), holdings(張), etf`）→ 同步至 `gdrive:etf_daily_holdings/`。
20260930 (含) 以前為 CMoney 原檔；之後由投信資料轉出，只含判定得出基準日的 ETF (國內個股不足 8 檔的槓桿/反向/海外/債券型不轉)，且沒有現金列、期貨代號不帶合約月份。

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

## 📁 material_info/ — 全市場重大訊息

爬 MOPS「當日重大訊息」(t05st02) 全日清單 + 逐筆內文，涵蓋上市/上櫃/興櫃/公開發行。**07:00~23:50 每 10 分鐘輪詢新訊息；07:20 回掃近 3 天 + OpenAPI 對帳**。只存本機 D 槽，不同步雲端。

| 檔案 | 用途 |
|---|---|
| `download_material_info.py` | `--mode poll` 抓今天清單只補新訊息內文；`--mode daily` 回掃近 3 天、重抓缺內文、用 TWSE/TPEx OpenAPI `t187ap04` (前一日上市/上櫃) 對帳補漏；`--start/--end` 區間回補 (四市場) |
| `backfill_t51sb10.py` | 歷史回補：MOPS「重大訊息主旨全文檢索」(t51sb10_q1) 市場別查詢 上市/上櫃，含爬蟲禮儀 (間隔/長休/被擋冷卻/斷點續傳) |
| `store.py` | Parquet 儲存層：月檔 upsert (跨行程寫入鎖)、區間/增量讀取 |
| `query_material_info.py` | 查閱工具：依日期/公司/市場/關鍵字篩選，可匯出 CSV 給 Excel、`--fetched-after` 取增量 (推播用) |
| `run_material_info.sh` | cron 啟動腳本 (`poll`/`daily`)，經 `common/run_with_alert.sh` 告警、flock 防重疊 |

**資料存放**：`D:/mops/material_info/data/重大訊息_YYYY-MM.parquet`（依發言日期月份分檔，zstd，約為 CSV 的 1/3.7）。欄位：公司代號、公司簡稱、發布時間、主旨、說明 + 市場別/符合條款/事實發生日/發言人/來源/MOPS鍵/抓取時間。

> MOPS 首頁「即時重大訊息」(t05sr01_1) API 只回最新 20 筆，故改用 t05st02 全日清單；DR 公司內文走 `t59sb01_detail`。

---

## 📁 news/ — 財經新聞

全量收集 (不篩選)，存 `D:/mops/news/data/新聞_YYYY-MM.parquet`（含完整內文）。**全天每 15 分鐘輪詢**。

| 檔案 | 用途 |
|---|---|
| `download_news.py` | `poll`：鉅亨 API + 經濟日報/工商時報/科技新報/自由財經/中央社/MoneyDJ RSS，新連結逐篇抓原文頁全文；`backfill`：鉅亨逐日回補 (可續傳)；`fulltext`：補抓過短內文 |
| `store.py` | 新聞 Parquet 儲存層 (鍵 = 連結) |
| `tag_news.py` | 依 `event_rules/rules.py` 貼事件標籤 → `D:/mops/news/tagged/` |
| `clean_labels.py` | 整理人工標註檔 `daily_script/data/重要新聞.csv` → `D:/mops/news/labels/` |
| `run_news.sh` | cron 啟動腳本 |

## 📁 event_rules/ — 事件標籤與新鮮度評分 (純規則，不經 AI 模型)

| 檔案 | 用途 |
|---|---|
| `rules.py` | 事件類別 (多標籤)、新鮮度用語、重量級對象的文字模式；調整方向只改這個檔 |
| `numparse.py` | 金額 (含國字大寫/仟萬億)、百分比、股數、民國日期解析 |
| `freshness.py` | 重訊事件 + 新聞標籤併表，算「標籤×對象類別」在台股過去一年出現天數等稀有度 + 用語/金額加減分 → `D:/mops/news/scored/新鮮度_YYYY-MM.parquet`、`review/新鮮度檢視_日期.csv` (人工回饋用)；權重在 `WEIGHTS` |
| `run_pipeline.sh` | 每日流程：重訊過濾 → 標籤/數據抽取 → 新聞標籤 → 評分 (cron 07:40/13:40/18:40/22:40) |

重訊側另有 `material_info/filter_material_info.py` (重發/例行/候選標記、公司行動階段) 與
`material_info/extract_events.py` (現增/私募/可轉債/減資/自結/庫藏股數據抽取)，輸出於 `D:/mops/material_info/derived/`。

---

## ⏰ 自動排程 (crontab)

| 時間 | 工作 |
|---|---|
| 06:00 每天 | `taifex/run_taifex_daily.sh` — 期交所期貨+選擇權下載 + 同步雲端 |
| 18:55 週一~五 | `etf/run_crawler.sh` — 投信 ETF 持股 + 轉 CMoney 格式 (18:55、20:40 兩輪) + 同步雲端 |
| 07:00~23:50 每 10 分 / 07:20 每天 | `material_info/run_material_info.sh poll` / `daily` — 重大訊息輪詢 / 回掃對帳 (僅存 D 槽) |
| 全天每 15 分 | `news/run_news.sh` — 財經新聞輪詢 (僅存 D 槽) |
| 07:40/13:40/18:40/22:40 | `event_rules/run_pipeline.sh` — 重訊+新聞標籤與新鮮度評分 |
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
