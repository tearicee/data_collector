#!/usr/bin/env python3
"""檢查 FinMind token 是否仍為有效的 SponsorPro 等級。
有效印出 'OK'，否則印出失效原因。供 daily_update.sh 判斷是否繼續下載。
"""
import sys
from pathlib import Path

import requests
from dotenv import dotenv_values

env = dotenv_values(Path(__file__).resolve().parent / ".env")
token = env.get("finmind_api_key", "").strip()
if not token:
    print("NO_TOKEN")
    sys.exit(0)

try:
    r = requests.get(
        "https://api.web.finmindtrade.com/v2/user_info",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    j = r.json()
except Exception as e:
    # 網路/伺服器暫時性問題：不視為到期，讓今天照常嘗試下載
    print(f"CHECK_ERROR:{e}")
    sys.exit(0)

sp = j.get("SponsorInfo", {}) or {}
spp = j.get("SponsorProInfo", {}) or {}
level = j.get("level", 0)


def _valid(info):
    # 訂閱有效：狀態 200 且非「已到期」
    return info.get("status_code") == 200 and info.get("msg") != "已到期"


# 付費等級仍有效即繼續下載：SponsorPro (level>=4, 20000/hr) 或 Sponsor (level>=3, 6000/hr) 任一有效即可。
# 2026-06-30 SponsorPro 到期後改吃 Sponsor，故不再硬性要求 SponsorPro。
if _valid(spp) or _valid(sp):
    print("OK")
else:
    print(f"EXPIRED level={level} "
          f"sponsor_msg={sp.get('msg')!r} sponsor_exp={sp.get('subscription_expired_date')} "
          f"pro_msg={spp.get('msg')!r} pro_exp={spp.get('subscription_expired_date')}")
