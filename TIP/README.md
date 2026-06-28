# TIP 台灣指數公司技術通知下載器

每天從台灣指數公司「技術通知」下載最新報告，解析後彙整進 CSV。

- 來源頁：<https://taiwanindex.com.tw/downloads/technical_notice>（Nuxt SSR，`?page=N` 分頁，共 1200+ 筆）
- 下載 API：`https://backend.taiwanindex.com.tw/api/downloadFile/TechnicalNotices/{id}/tw`
- 資料存放（D 槽）：`/mnt/d/taiwanindex/technical_notice/`
- 雲端備份：`gdrive:taiwanindex/technical_notice/`

## 下載對象（依「檔案類別」篩選）

| 檔案類別 | 內容 | 解析器 | 輸出 CSV |
|---|---|---|---|
| `定審結果` | 成分股審核結果（納入/刪除清單） | `parse_notice.py` | `成分股調整紀錄.csv` |
| `指數定審期程表` | 指數定期審核日程表（各指數公告日/生效日） | `parse_schedule.py` | `review_schedule.csv` |

## CSV 欄位

- **成分股調整紀錄.csv**（比照 `成分股調整紀錄.xlsx` A:G 欄位）：
  `stock_id, review_date(定審資料日), public_date(公告日), effective_date(生效日), type(add/delete), etf(ETF代號), is_new`
  - `etf` 由 `index_etf_map.csv`（指數名稱→ETF 代號對照表）填入；一指數對多 ETF 則展開多列，無對照者留空（資料保留）。
- **review_schedule.csv**：`notice_id, category, file_date, report_title, update_date, index_name, announce_date, effective_date`

## 指數→ETF 對照表（index_etf_map.csv）

對應 `etf/etf_crawler.py` 收集的 ETF 中，**追蹤臺灣指數公司(TIP)指數**者共 15 支才有定審結果：
00929 00919 00918 00713 00940 00939 00900 00932 00923 00915 00936 00934 00733 00913 00881。
其餘為 FTSE/MSCI/ICE FactSet 指數（0050 0052 0056 0057 00878 00891）或主動式 ETF
（00982A 00981A 00400A 00403A 00405A 00991A 00992A），無 TIP 定審報告。
新增/修改對照只需編輯此 CSV，重跑即生效。

## 檔案

| 檔案 | 說明 |
|---|---|
| `download_notice.py` | 主下載器：抓列表 → 篩目標類別 → 下載 PDF → 解析 → append CSV（`processed_ids.json` 去重） |
| `parse_notice.py` | 解析「成分股審核結果」PDF |
| `parse_schedule.py` | 解析「指數定期審核日程表」PDF |
| `run_tip.sh` | cron 包裝（`--days 2` 容錯）+ gdrive 備份 |
| `backup_to_gdrive.sh` | rclone 備份到 Google Drive |

## 用法

```bash
VENV=/home/tearicee/data_collector/.venv/bin/python
$VENV download_notice.py                 # 抓「今天」發布的目標報告
$VENV download_notice.py --date 20260626 # 抓指定日期
$VENV download_notice.py --days 7        # 抓最近 7 天（含今天）
$VENV download_notice.py --date 20260626 --force  # 忽略去重重抓
```

## 排程（cron）

```cron
# 交易日盤後 19:30 下載當天技術通知
30 19 * * 1-5 /home/tearicee/data_collector/TIP/run_tip.sh
```

> 註：使用 D 槽共用 `data_collector/.venv`；需 `pdfplumber`、`requests`。
> 註：成分股「納入(0)/刪除(0)：無」者解析 0 列為正常（該指數本次無異動）。
