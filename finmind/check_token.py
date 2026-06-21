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

spp = j.get("SponsorProInfo", {}) or {}
level = j.get("level", 0)
# SponsorPro 仍有效：level>=4 且 SponsorProInfo 狀態 200 且非「已到期」
if level >= 4 and spp.get("status_code") == 200 and spp.get("msg") != "已到期":
    print("OK")
else:
    print(f"EXPIRED level={level} msg={spp.get('msg')!r} "
          f"expired_date={spp.get('subscription_expired_date')}")
