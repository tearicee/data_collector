#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日人工檢討用清單 (使用者逐則回覆 要/不要 → 回頭調規則，直到推播內容穩定)
  1. 事件分 ≥7 (會推播)   2. 事件分 6~7 (差一點)   3. 低分但熱度高 (可能漏掉)
同事件多篇報導只列分數最高的一篇。
用法：python event_rules/review_candidates.py "2026-10-03 13:00:00"   (列出此時間之後的)
"""
import glob
import sys

import pandas as pd

since = sys.argv[1] if len(sys.argv) > 1 else (pd.Timestamp.now() - pd.Timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
d=pd.concat(pd.read_parquet(f) for f in sorted(glob.glob('/mnt/d/mops/news/scored/新鮮度_*.parquet'))[-2:])
names=pd.read_parquet('/mnt/d/finmind_data/TaiwanStockInfo/TaiwanStockInfo.parquet').drop_duplicates('stock_id').set_index('stock_id')['stock_name']
d=d[d['time']>=since].copy()
print('自',since,'評分筆數',len(d),'最新',d.time.max(),' 事件分分布:', d['event_score'].value_counts().sort_index(ascending=False).head(9).to_dict())
def nm(s): return ' '.join(f"{c}{names.get(c,'')}" for c in str(s).split(',')[:3] if c)
for lo,hi in [(7,11),(6,7)]:
    x=d[(d['event_score']>=lo)&(d['event_score']<hi)].sort_values('event_score',ascending=False).drop_duplicates('event_id').sort_values('time')
    print(f'\n===== 事件分 [{lo},{hi}) 共 {len(x)} 則')
    for r in x.itertuples():
        print(f"{str(r.time)[5:16]} | {r.event_score} 熱{r.heat} | {r.source} | {nm(r.stocks)} | {r.direction or '-'} | {r.title[:64]}\n      理由: {str(r.reasons)[:240]}")
x=d[(d.event_score<6)&(d.heat>=5)].sort_values('heat',ascending=False).drop_duplicates('event_id').head(15)
print('\n===== 低分但熱度高 (可能漏掉)')
for r in x.itertuples(): print(f"{str(r.time)[5:16]} | {r.event_score} 熱{r.heat} | {r.source} | {nm(r.stocks)} | {r.title[:60]} | {str(r.heat_reason)[:70]}")
