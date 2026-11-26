"""Continuous Telegram Bot runner using long-polling.

Eliminates the need for public URLs or ngrok tunnels in local environments.
Receives consignment queries, auto-subscribes users, and processes account linking.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv

load_dotenv(dotenv_path=BASE_DIR / ".env", override=False)

from api.telegram_routes import (
    CN_PATTERN,
    TelegramLinkingError,
    _auto_subscribe_telegram_user,
    _format_shipment_telegram_card,
    _send_telegram_reply,
    parse_telegram_update,
    verify_and_consume_telegram_link,
)
from database.shipment_repository import get_shipment_by_tracking_number

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] [telegram-bot] %(message)s",
)
logger = logging.getLogger("telegram-bot")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN is not configured in .env.")


def delete_webhook_if_present(client: httpx.Client) -> None:
    try:
        resp = client.post(
            f"https://api.telegram.org/bot{TOKEN}/deleteWebhook",
            json={"drop_pending_updates": False},
            timeout=10.0,
        )
        if resp.status_code == 200:
            logger.info("Webhook cleared; using direct long-polling.")
    except Exception as exc:
        logger.warning("Could not clear webhook: %s", exc)


def handle_update(update: dict) -> None:
    msg = update.get("message") or update.get("callback_query", {}).get("message") or {}
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if not chat_id:
        return

    raw_text = (msg.get("text") or "").strip()
    first_name = msg.get("from", {}).get("first_name", "there")

    # Check for linking challenge in /start
    try:
        _, start_param = parse_telegram_update(update)
    except TelegramLinkingError:
        start_param = None

    if start_param:
        try:
            res = verify_and_consume_telegram_link(start_param, update)
            logger.info("Consumed linking challenge for chat %s (%s)", chat_id, res.get("owner_type"))
            _send_telegram_reply(
                chat_id,
                "✅ *Telegram account successfully linked!*\nYou will now receive automatic notifications for your shipments.",
            )
        except Exception as exc:
            logger.warning("Linking error for chat %s: %s", chat_id, exc)
            _send_telegram_reply(chat_id, f"⚠️ *Linking failed:* {exc}")
        return

    # Check if text is a 13-character consignment number
    if CN_PATTERN.match(raw_text):
        cn = raw_text.upper()
        logger.info("Received consignment query '%s' from chat %s", cn, chat_id)
        shipment = get_shipment_by_tracking_number(cn, "india-post")
        if shipment:
            _auto_subscribe_telegram_user(chat_id, shipment["shipment_id"])
            card = _format_shipment_telegram_card(shipment)
            _send_telegram_reply(chat_id, card)
            logger.info("Sent shipment tracking card to chat %s", chat_id)
        else:
            _send_telegram_reply(
                chat_id,
                f"Consignment `{cn}` is not yet in our database.\n"
                "You can track it in the web assistant or subscribe for automatic updates!",
            )
        return

    # Standard greeting / help
    if raw_text.lower() in ("/start", "start", "hi", "hello", "help"):
        logger.info("Received greeting from %s (%s)", first_name, chat_id)
        _send_telegram_reply(
            chat_id,
            f"👋 *Hello {first_name}!* Welcome to the *India Post AI Shipment Assistant*.\n\n"
            "Send me any 13-character India Post Consignment Number (e.g. `CU488060679IN`) to check its status, "
            "or link your alerts via the web assistant!",
        )


def main():
    logger.info("Starting Telegram Bot Polling Runner...")
    stop = False

    def sig_handler(sig, frame):
        nonlocal stop
        logger.info("Stopping Telegram Bot runner...")
        stop = True

    signal.signal(signal.SIGINT, sig_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, sig_handler)

    offset = None
    with httpx.Client(timeout=35.0) as client:
        delete_webhook_if_present(client)
        while not stop:
            try:
                params = {"timeout": 25}
                if offset is not None:
                    params["offset"] = offset

                resp = client.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", params=params)
                if resp.status_code != 200:
                    logger.warning("Telegram getUpdates returned %s: %s", resp.status_code, resp.text)
                    time.sleep(2)
                    continue

                data = resp.json()
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    try:
                        handle_update(update)
                    except Exception as exc:
                        logger.error("Error processing update %s: %s", update.get("update_id"), exc)

            except (httpx.TimeoutException, httpx.NetworkError):
                continue
            except Exception as exc:
                if not stop:
                    logger.warning("Unexpected error during polling: %s", exc)
                    time.sleep(2)

    logger.info("Telegram Bot Polling Runner exited cleanly.")


if __name__ == "__main__":
    main()
