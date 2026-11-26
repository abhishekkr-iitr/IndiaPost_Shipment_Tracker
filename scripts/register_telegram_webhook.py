"""CLI helper to verify Telegram Bot token and register the inbound webhook.

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET directly from .env,
verifies bot authentication via Telegram API, and configures the webhook URL.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv

load_dotenv(dotenv_path=BASE_DIR / ".env", override=False)


def check_bot_token(token: str) -> dict:
    """Verify bot token by calling getMe."""
    url = f"https://api.telegram.org/bot{token.strip()}/getMe"
    req = urllib.request.Request(url, headers={"User-Agent": "IndiaPost-Assistant/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            err_json = json.loads(body)
            desc = err_json.get("description", str(exc))
        except Exception:
            desc = str(exc)
        raise RuntimeError(f"Telegram API Error ({exc.code}): {desc}") from exc


def register_webhook(token: str, webhook_url: str, secret_token: str | None = None) -> dict:
    """Register webhook URL and secret token with Telegram Bot API."""
    clean_url = webhook_url.strip()
    if not clean_url.endswith("/api/webhooks/telegram"):
        clean_url = clean_url.rstrip("/") + "/api/webhooks/telegram"

    payload = {"url": clean_url}
    if secret_token and secret_token.strip():
        payload["secret_token"] = secret_token.strip()

    data = json.dumps(payload).encode("utf-8")
    url = f"https://api.telegram.org/bot{token.strip()}/setWebhook"
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "IndiaPost-Assistant/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_webhook_info(token: str) -> dict:
    """Retrieve current registered webhook status."""
    url = f"https://api.telegram.org/bot{token.strip()}/getWebhookInfo"
    req = urllib.request.Request(url, headers={"User-Agent": "IndiaPost-Assistant/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Register Telegram Webhook for India Post Assistant")
    parser.add_argument("--url", type=str, help="Public HTTPS Tunnel / Domain URL (e.g. https://xxx.trycloudflare.com)")
    parser.add_argument("--info-only", action="store_true", help="Only check bot token and current webhook info")
    args = parser.parse_args()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()

    print("==================================================")
    print("  TELEGRAM WEBHOOK REGISTRATION UTILITY")
    print("==================================================")

    if not token:
        print("[ERROR] TELEGRAM_BOT_TOKEN is missing in .env.")
        print("Please add your token from @BotFather to your .env file.")
        sys.exit(1)

    print("1. Verifying bot token with Telegram API...")
    try:
        bot_info = check_bot_token(token)
        username = bot_info.get("result", {}).get("username", "Unknown")
        first_name = bot_info.get("result", {}).get("first_name", "Unknown")
        print(f"   [SUCCESS] Connected to bot: @{username} ({first_name})")
    except Exception as exc:
        print(f"   [FAILED] {exc}")
        print("\nTroubleshooting:")
        print(" - Verify you copied the token completely from @BotFather.")
        print(" - Make sure there are no typos, extra spaces, or brackets '<>' around the token.")
        print(" - In @BotFather, check /mybots -> your bot -> 'API Token' to verify or regenerate.")
        sys.exit(1)

    if args.info_only or not args.url:
        print("\n2. Checking current webhook status...")
        info = get_webhook_info(token)
        result = info.get("result", {})
        print(f"   Registered URL: {result.get('url') or '(None registered)'}")
        print(f"   Pending Updates: {result.get('pending_update_count', 0)}")
        if not args.url:
            print("\nTo set a new webhook, run:")
            print("  .\\venv\\Scripts\\python.exe scripts/register_telegram_webhook.py --url https://<your-subdomain>.trycloudflare.com")
        sys.exit(0)

    print(f"\n2. Registering webhook with target: {args.url}...")
    try:
        res = register_webhook(token, args.url, secret)
        print(f"   [SUCCESS] Telegram response: {res.get('description', 'Webhook set')}")
    except Exception as exc:
        print(f"   [FAILED] Could not register webhook: {exc}")
        sys.exit(1)

    print("\n3. Confirming registration with Telegram...")
    info = get_webhook_info(token)
    result = info.get("result", {})
    print(f"   Active Webhook URL: {result.get('url')}")
    print(f"   Custom Certificate: {result.get('has_custom_certificate')}")
    print("==================================================")
    print("Ready! You can now test the live linking in the browser UI.")


if __name__ == "__main__":
    main()
