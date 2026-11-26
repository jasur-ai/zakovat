#!/usr/bin/env python3
"""Register the Telegram webhook after Vercel deployment.

Reads secrets from the environment only. Never paste tokens into chat or repo.
    export BOT_TOKEN='...'
    export WEBHOOK_SECRET='...'
    export WEBHOOK_URL='https://your-project.vercel.app'
    python3 scripts/set_webhook.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request


def main() -> int:
    token = os.getenv("BOT_TOKEN")
    url = os.getenv("WEBHOOK_URL")
    secret = os.getenv("WEBHOOK_SECRET", "")
    if not token or not url:
        print("BOT_TOKEN va WEBHOOK_URL muhit o‘zgaruvchilari kerak.", file=sys.stderr)
        return 1
    endpoint = url.rstrip("/") + "/api/index"
    payload = {
        "url": endpoint,
        "allowed_updates": json.dumps(["message"]),
        "drop_pending_updates": "true",
    }
    if secret:
        payload["secret_token"] = secret
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/setWebhook",
        data=urllib.parse.urlencode(payload).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
