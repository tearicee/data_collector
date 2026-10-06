#!/usr/bin/env python3
"""data_collector 共用 Discord 告警模組 (Incoming Webhook)。

改寫自 ex_dividend/notify_discord.py，供整個 data_collector 的爬蟲任務發送
「失敗告警」與「每日健檢摘要」。

設定:
  在 data_collector/.env 內填入 (見 .env.example):
    DISCORD_WEBHOOK_URL_ALERT=https://discord.com/api/webhooks/xxx/yyy

呼叫端:
  from common import notify_discord
  notify_discord.post("報表內容")                 # 預設送到 ALERT 頻道 (程式碼框)
  notify_discord.alert("MOPS 主檔爬蟲", "HTTP 500", exit_code=1)   # 失敗告警

CLI (供 shell wrapper 使用):
  echo "訊息" | python common/notify_discord.py              # stdin → 程式碼框
  echo "訊息" | python common/notify_discord.py --raw        # stdin → 純文字 (不包框)

Discord 單則訊息上限 2000 字, 長內容自動依行切成多則。
"""
from __future__ import annotations
import argparse
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import alert_log  # noqa: E402

# BASE_DIR = data_collector 專案根 (common 的上一層)，.env 放在這裡
BASE_DIR = Path(__file__).resolve().parent.parent
MAX_LEN = 1900          # 留餘裕給 ``` 框與換行 (上限 2000)
DEFAULT_ENV_VAR = "DISCORD_WEBHOOK_URL_ALERT"


def _chunks(text: str, limit: int = MAX_LEN):
    """依行切塊, 每塊不超過 limit 字。單行過長會硬切。"""
    buf, size = [], 0
    for line in text.splitlines():
        if len(line) > limit:                       # 極端情況: 硬切長行
            if buf:
                yield "\n".join(buf); buf, size = [], 0
            for i in range(0, len(line), limit):
                yield line[i:i + limit]
            continue
        if size + len(line) + 1 > limit and buf:
            yield "\n".join(buf); buf, size = [], 0
        buf.append(line); size += len(line) + 1
    if buf:
        yield "\n".join(buf)


def _resolve_webhook(webhook_url: str | None, env_var: str) -> str | None:
    if webhook_url is None:
        load_dotenv(BASE_DIR / ".env")
        webhook_url = os.environ.get(env_var)
    return webhook_url


def post(report: str, webhook_url: str | None = None,
         env_var: str = DEFAULT_ENV_VAR, code_block: bool = True) -> int:
    """貼出內容, 回傳成功送出的訊息則數。

    webhook_url 省略時從 data_collector/.env 讀 env_var 指定的變數。
    code_block=False 時不包 ``` 框 (適合帶 emoji / markdown 的告警標題)。
    """
    webhook_url = _resolve_webhook(webhook_url, env_var)
    if not webhook_url:
        raise RuntimeError(
            f"未設定 {env_var} (請在 data_collector/.env 填入 webhook 網址)")

    parts = list(_chunks(report)) or [""]
    sent = 0
    for idx, part in enumerate(parts, 1):
        if code_block:
            content = f"```\n{part}\n```"
        else:
            content = part
        if len(parts) > 1:
            content += f"`({idx}/{len(parts)})`"
        r = requests.post(webhook_url, json={"content": content}, timeout=30)
        if r.status_code == 429:                    # 被限流, 等 retry_after 後重送
            wait = r.json().get("retry_after", 1)
            time.sleep(float(wait) + 0.5)
            r = requests.post(webhook_url, json={"content": content}, timeout=30)
        r.raise_for_status()
        sent += 1
        time.sleep(0.5)                             # 溫和節流, 避免連發被限流
    return sent


def alert(job: str, message: str, exit_code: int | None = None,
          log_tail: str = "", env_var: str = DEFAULT_ENV_VAR) -> int:
    """發送標準格式的失敗告警。標題行帶 emoji + job 名 (不包框)，
    詳細訊息 / log 尾段包在程式碼框內。發送失敗只印錯誤、不拋例外
    (告警本身不該中斷主任務)。"""
    header = f"🔴 **爬蟲失敗告警** — `{job}`"
    if exit_code is not None:
        header += f"  (exit={exit_code})"
    body = message.strip()
    tail = alert_log.compact_tail(log_tail)          # 去掉進度雜訊、只留最後幾行
    if tail:
        body += "\n\n--- log 尾段 ---\n" + tail
    n = 0
    try:
        n = post(header, env_var=env_var, code_block=False)
        n += post(body, env_var=env_var, code_block=True)
    except Exception as e:                           # noqa: BLE001
        print(f"[notify_discord][error] 告警送出失敗: {e}", file=sys.stderr)
    alert_log.record("data_collector", job, message, exit_code, log_tail, sent=n > 0)
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="送訊息到 data_collector 告警頻道")
    ap.add_argument("--raw", action="store_true", help="不包程式碼框 (純文字)")
    ap.add_argument("--env-var", default=DEFAULT_ENV_VAR,
                    help=f"要讀的 webhook 變數 (預設 {DEFAULT_ENV_VAR})")
    args = ap.parse_args()

    data = sys.stdin.read() if not sys.stdin.isatty() else "Discord 告警頻道測試訊息 ✅"
    n = post(data, env_var=args.env_var, code_block=not args.raw)
    print(f"已送出 {n} 則訊息到 Discord")
