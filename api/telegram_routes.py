"""Inbound Telegram webhook router for bot updates and account linking.

Verifies inbound updates, extracts trusted Telegram chat IDs server-side,
and atomically binds single-use linking challenge tokens to registered
or guest notification identities.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hmac
import logging
import os
import re
from typing import Any

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from pydantic import BaseModel

from database.shipment_repository import get_shipment_by_tracking_number
from notifications.telegram_linking import (
    TelegramLinkingError,
    parse_telegram_update,
    verify_and_consume_telegram_link,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhooks/telegram", tags=["telegram-webhook"])

CN_PATTERN = re.compile(r"^[A-Za-z]{2}\d{9}[A-Za-z]{2}$")


def _send_telegram_reply(chat_id: str, text: str) -> None:
    """Send an outbound reply back to the Telegram chat."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = httpx.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=8.0,
        )
        if resp.status_code != 200:
            logger.warning("Telegram sendMessage returned %s: %s", resp.status_code, resp.text)
    except Exception as exc:
        logger.warning("Could not send Telegram reply: %s", exc)


def _format_checkpoint_timestamp(raw_date: str) -> str:
    if not raw_date:
        return ""
    try:
        clean = raw_date.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        return dt.strftime("%d %b %H:%M")
    except Exception:
        if "T" in raw_date:
            parts = raw_date.split("T")
            return f"{parts[0]} {parts[1][:5]}"
        return raw_date[:16]


def _get_shipment_checkpoints(shipment: dict, limit: int = 20) -> tuple[list[dict], int]:
    cps = shipment.get("checkpoints")
    if isinstance(cps, list) and cps:
        total = len(cps)
        return cps[-limit:], total

    shipment_id = shipment.get("shipment_id")
    if not shipment_id:
        return [], 0

    try:
        from database.db_connection import get_db_connection
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM checkpoints WHERE shipment_id = ?;", (shipment_id,))
            total_row = cur.fetchone()
            total = total_row[0] if total_row else 0
            if total == 0:
                return [], 0

            cur.execute(
                """
                SELECT event_date, location, status, message
                FROM checkpoints
                WHERE shipment_id = ?
                ORDER BY event_date DESC, checkpoint_id DESC
                LIMIT ?;
                """,
                (shipment_id, limit),
            )
            rows = cur.fetchall()
            fetched = [
                {"date": r[0], "location": r[1], "status": r[2], "message": r[3]}
                for r in rows
            ]
            fetched.reverse()
            return fetched, total
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("Could not load checkpoints for shipment card: %s", exc)
        return [], 0


def _format_shipment_telegram_card(shipment: dict, limit: int = 20) -> str:
    cn = shipment.get("tracking_number", "")
    status_val = shipment.get("current_status", "In Transit")
    msg = shipment.get("status_message") or status_val
    origin = shipment.get("origin") or "India"
    dest = shipment.get("destination") or "India"

    status_icon = "✅" if status_val.lower() == "delivered" else "🚚"

    cps, total_count = _get_shipment_checkpoints(shipment, limit=limit)
    checkpoints_section = ""
    if cps:
        header = f"*Recent Checkpoints (Latest {len(cps)} of {total_count}):*" if total_count > len(cps) else f"*Checkpoints ({total_count}):*"
        lines = []
        for cp in cps:
            date_str = _format_checkpoint_timestamp(cp.get("date") or "")
            loc = (cp.get("location") or "").strip().replace("*", "").replace("_", " ").replace("`", "")
            cp_msg = (cp.get("message") or cp.get("status") or "").strip().replace("*", "").replace("_", " ").replace("`", "")
            if loc and loc.lower() != "none":
                line = f"• *{date_str}* ({loc}): {cp_msg}" if date_str else f"• ({loc}): {cp_msg}"
            else:
                line = f"• *{date_str}*: {cp_msg}" if date_str else f"• {cp_msg}"
            lines.append(line)

        # Safeguard to keep total message within Telegram's 4096 character limit
        while len("\n".join(lines)) > 3200 and len(lines) > 1:
            lines.pop(0)

        checkpoints_section = f"\n{header}\n" + "\n".join(lines) + "\n━━━━━━━━━━━━━━━━━━━━"

    return f"""📦 *India Post Consignment Tracking*
━━━━━━━━━━━━━━━━━━━━
*Consignment:* `{cn}`
*Carrier:* India Post
*Status:* {status_icon} *{status_val.upper()}*
*Detail:* {msg}
*Origin:* {origin}
*Destination:* {dest}
━━━━━━━━━━━━━━━━━━━━{checkpoints_section}
🔔 _Subscribed to automatic alerts for this shipment_
_India Post AI Shipment Assistant_"""


def _auto_subscribe_telegram_user(chat_id: str, shipment_id: int) -> None:
    """Ensure this Telegram chat_id is subscribed to automatic updates for this shipment."""
    try:
        from database.guest_notification_repository import (
            create_guest_notification_identity,
            get_guest_notification_identity,
            create_guest_shipment_subscription,
            save_guest_channel_preference,
        )
        guest_identity_id = f"tg_{chat_id}"
        if not get_guest_notification_identity(guest_identity_id):
            create_guest_notification_identity(
                notes=f"Auto-created from Telegram bot for chat {chat_id}",
                guest_identity_id=guest_identity_id,
            )

        now_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save_guest_channel_preference(
            guest_identity_id=guest_identity_id,
            channel="telegram",
            recipient=str(chat_id),
            enabled=True,
            verified_at=now_ts,
            consent_at=now_ts,
        )
        create_guest_shipment_subscription(
            guest_identity_id=guest_identity_id,
            shipment_id=shipment_id,
        )
        logger.info("Auto-subscribed Telegram user %s to shipment %s", chat_id, shipment_id)
    except Exception as exc:
        logger.warning("Could not auto-subscribe telegram user %s to shipment %s: %s", chat_id, shipment_id, exc)


class TelegramWebhookResponse(BaseModel):
    ok: bool
    status: str
    chat_id: str | None = None
    owner_type: str | None = None
    error: str | None = None


@router.post("", response_model=TelegramWebhookResponse)
async def handle_telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(
        default=None,
        alias="X-Telegram-Bot-Api-Secret-Token",
    ),
) -> TelegramWebhookResponse:
    """Handle inbound webhook updates from Telegram Bot API."""
    expected_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if expected_secret:
        if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
            x_telegram_bot_api_secret_token.strip(),
            expected_secret.strip(),
        ):
            logger.warning("Telegram webhook rejected: invalid or missing secret token header.")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid Telegram webhook secret token.",
            )

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payload must be valid JSON.",
        )

    try:
        chat_id, start_param = parse_telegram_update(payload)
    except TelegramLinkingError as exc:
        logger.info("Telegram update did not contain valid parseable message: %s", exc)
        return TelegramWebhookResponse(
            ok=True,
            status="ignored",
            error=str(exc),
        )

    if not start_param:
        msg_obj = payload.get("message") or payload.get("callback_query", {}).get("message") or {}
        raw_text = (msg_obj.get("text") or "").strip()
        first_name = msg_obj.get("from", {}).get("first_name", "there")

        # 1. If user sent a 13-character Consignment Number (e.g. CU488060679IN)
        if CN_PATTERN.match(raw_text):
            shipment = get_shipment_by_tracking_number(raw_text.upper(), "india-post")
            if shipment:
                _auto_subscribe_telegram_user(chat_id, shipment["shipment_id"])
                card = _format_shipment_telegram_card(shipment)
                _send_telegram_reply(chat_id, card)
            else:
                _send_telegram_reply(
                    chat_id,
                    f"Consignment `{raw_text.upper()}` is not in our database yet. You can track it in the assistant web app or subscribe for updates!",
                )
        # 2. If user sent /start, hi, hello, or help
        elif raw_text.lower() in ("/start", "start", "hi", "hello", "help"):
            _send_telegram_reply(
                chat_id,
                f"👋 *Hello {first_name}!* Welcome to the *India Post AI Shipment Assistant*.\n\n"
                "Send me any 13-character India Post Consignment Number (e.g. `CU488060679IN`) to check its status, "
                "or link your alerts via the web assistant!",
            )

        return TelegramWebhookResponse(
            ok=True,
            status="ignored",
            chat_id=chat_id,
        )

    # Attempt to consume link challenge
    try:
        result = verify_and_consume_telegram_link(start_param, payload)
        return TelegramWebhookResponse(
            ok=True,
            status="linked",
            chat_id=chat_id,
            owner_type=result["owner_type"],
        )
    except TelegramLinkingError as exc:
        logger.warning("Telegram linking challenge failed: %s", exc)
        return TelegramWebhookResponse(
            ok=False,
            status="link_failed",
            chat_id=chat_id,
            error=str(exc),
        )
