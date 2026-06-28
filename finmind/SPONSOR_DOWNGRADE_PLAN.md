# SponsorPro → Sponsor 降級轉換計畫

**觸發時機**：SponsorPro 到期 **2026-06-30**(查 `user_info` SponsorProInfo.subscription_expired_date)，
2026-07-01 起降為 Sponsor。或當 bulk(storage_objects)開始回 401/403 即執行本計畫。

**API 上限**：SponsorPro 20,000/hr → **Sponsor 6,000/hr**。

---

## 影響範圍：只有 4 個 storage_objects「bulk 整日全市場」資料集

bulk 失效後改逐單位查(其餘 /data「不帶 data_id 取整日全市場」Sponsor 仍可用，含期/權 tick)。

| 資料集 | 降級前(bulk, 1 req/日) | 降級後(逐單位) | 用 |
|--------|:---:|:---:|---|
| TaiwanStockPriceTick | download_tick.py | ~3,112 req 逐股票 | `download_tick_by_stock.py` |
| TaiwanStockKBar | download_tick.py | ~3,112 req 逐股票 | `download_tick_by_stock.py` |
| TaiwanStockTradingDailyReport | download_tick.py | ~1,063 req 逐券商 | `download_daily_report_by_broker.py` |
| TaiwanStockWarrantTradingDailyReport | download_tick.py | ~1,063 req 逐券商 | `download_daily_report_by_broker.py` |

降級後總量 ~7,900 req/日(其中這 4 集 ~7,500)。

---

## 前置作業(2026-06-28 已完成備妥)

- ✅ `build_stock_ids.py` → `stock_ids.parquet`(TaiwanStockInfo ∪ 近期 bulk 觀察，目前 3,112 檔)
- ✅ `build_trader_ids.py` → `securities_trader_ids.parquet`(1,063 券商代碼)
- ✅ `download_tick_by_stock.py`(PriceTick/KBar 逐股票，欄位已對齊 bulk，已測試)
- ✅ `download_daily_report_by_broker.py`(分點/權證分點逐券商，已測試)
- ✅ `daily_sponsor_fallback.sh <dataset>`(降級後每日 wrapper：token 檢查/防重複/近 7 天視窗 skip-existing/blackout)

---

## 切換步驟(降級當天執行)

**1. 刷新代號清單**(趁仍有權限)：
```bash
cd /home/tearicee/data_collector/finmind
.venv/python build_stock_ids.py      # 實際: /home/tearicee/data_collector/.venv/bin/python
.venv/python build_trader_ids.py
```

**2. 停用現有腳本內的 bulk 下載**(註解掉/停用 cron，保留其餘 /data 資料集)：
- `daily_update.sh`：註解 **PriceTick** download_tick.py 行。⚠️ **保留** FuturesTick/OptionTick(download_data_tick.py，不受影響)。
- `daily_trading_report.sh`(22:00 獨立排程)：整支停用其 cron(改由 fallback 取代)。
- `daily_technical.sh`：註解 **KBar** download_tick.py 行。其餘技術面 /data 保留。
- `daily_chip.sh`：註解 **Warrant** download_tick.py 行。其餘籌碼 /data 保留。

**3. 加 4 條 cron**(分散時段，各小時 < 6,000/hr；資料發布後才跑)：
```cron
0  16 * * * /home/tearicee/data_collector/finmind/daily_sponsor_fallback.sh TaiwanStockPriceTick
0  18 * * * /home/tearicee/data_collector/finmind/daily_sponsor_fallback.sh TaiwanStockKBar
30 22 * * * /home/tearicee/data_collector/finmind/daily_sponsor_fallback.sh TaiwanStockTradingDailyReport
30 23 * * * /home/tearicee/data_collector/finmind/daily_sponsor_fallback.sh TaiwanStockWarrantTradingDailyReport
```

**4. 驗證**(隔日)：
```bash
tail /mnt/d/finmind_data/logs/daily_sponsor_*.log
# 確認當日 4 個檔有產生、列數約等於歷史 bulk(KBar 少 ~0.4% 收盤bar 屬正常)
```

---

## 降級後完整 cron 排程(各小時請求量 < 6,000)

| 時段 | 工作 | 主要消耗 |
|------|------|:---:|
| 16:00 | PriceTick 逐股票 (fallback) | ~3,112(~1.5hr) |
| 18:00 | KBar 逐股票 (fallback) | ~3,112(~1.5hr) |
| 20:00 | daily_update(僅剩 Futures/OptionTick) | ~2 |
| 21:00 | fundamental backfill | ~15 |
| 22:00 | daily_technical(已移除 KBar) | ~16 |
| 22:30 | TradingDailyReport 逐券商 (fallback) | ~1,063 |
| 23:30 | Warrant 逐券商 (fallback) | ~1,063 |
| 03:00 | dividend 更新 | ~348 |
| 04:00 | daily_chip(已移除 Warrant) | ~14 |
| 05:00 | convertible bond | ~3 |

---

## 已知限制(FinMind 端，不可避免)

- **逐股票 vs bulk**：每股少 1 列 = **14:30:00 收盤集合競價 bar/tick**。KBar ~0.4%、PriceTick ~0.01%；官方收盤價在 TaiwanStockPrice 另有。
- **逐券商權證分點**：~99%(到期/回收權證代碼查詢 API 無法解析，~0.97%)。個股分點則無損。
- PriceTick/KBar 逐股票資料量大、牆鐘 ~1-2hr，故各自獨立時段。

---

## 回復(若日後又升回 SponsorPro)

取消上述 4 條 fallback cron，取消 3 個腳本內 bulk 行的註解即可恢復 1 請求/日的 bulk 模式。
