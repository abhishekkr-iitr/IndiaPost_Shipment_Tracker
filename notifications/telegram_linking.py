"""Inbound Telegram linking challenge creation and webhook recipient verification.

Resolves Telegram chat IDs exclusively from trusted inbound Telegram webhook
updates and consumes short-lived single-use linking challenges to bind recipient
channels for registered users and guests.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from database.db_connection import get_db_connection
from database.notification_channel_preference_repository import save_channel_preference
from database.guest_notification_repository import save_guest_channel_preference


TELEGRAM_CHAT_ID_PATTERN = re.compile(r"^-?[1-9][0-9]{0,19}$")


class TelegramLinkingError(Exception):
    """Raised when Telegram link challenge verification fails."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


def create_telegram_link_challenge(
    owner_type: str,
    owner_id: int | str,
    expires_in_seconds: int = 900,
    bot_username: str = "IndiaPostShipmentBot",
) -> dict[str, Any]:
    """Create a short-lived single-use linking challenge bound to an owner."""
    if owner_type not in ("registered", "guest"):
        raise ValueError("owner_type must be either 'registered' or 'guest'.")

    user_id = None
    guest_identity_id = None
    if owner_type == "registered":
        if isinstance(owner_id, bool) or not isinstance(owner_id, int) or owner_id <= 0:
            raise ValueError("Registered owner_id must be a positive integer user_id.")
        user_id = owner_id
    else:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("Guest owner_id must be a non-empty guest_identity_id.")
        guest_identity_id = owner_id.strip()

    # Generate high-entropy secret token
    raw_code = secrets.token_urlsafe(16)
    code_hash = _hash_code(raw_code)

    challenge_id = str(uuid.uuid4())
    now_dt = datetime.now(timezone.utc)
    expires_dt = now_dt + timedelta(seconds=expires_in_seconds)
    expires_at = expires_dt.isoformat(timespec="seconds")
    now_ts = _now()

    connection = get_db_connection()
    try:
        connection.execute(
            """INSERT INTO telegram_link_challenges (
                challenge_id, owner_type, user_id, guest_identity_id,
                challenge_code_hash, expires_at, consumed_at, telegram_chat_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)""",
            (challenge_id, owner_type, user_id, guest_identity_id, code_hash, expires_at, now_ts),
        )
        connection.commit()

        clean_username = bot_username.lstrip("@").strip()
        deep_link = f"https://t.me/{clean_username}?start={raw_code}"

        return {
            "challenge_id": challenge_id,
            "link_code": raw_code,
            "deep_link": deep_link,
            "owner_type": owner_type,
            "owner_id": owner_id,
            "expires_at": expires_at,
        }
    finally:
        connection.close()


def parse_telegram_update(update_payload: dict[str, Any]) -> tuple[str, str | None]:
    """Extract trusted chat_id and start_parameter from a Telegram webhook payload.

    Never trusts browser or user input; extracts strictly from message.chat.id.
    """
    if not isinstance(update_payload, dict):
        raise TelegramLinkingError("Update payload must be a JSON object.")

    # Support standard message or callback_query message
    message = update_payload.get("message")
    if message is None and "callback_query" in update_payload:
        message = update_payload["callback_query"].get("message")

    if not isinstance(message, dict):
        raise TelegramLinkingError("Telegram update is missing a valid message object.")

    chat = message.get("chat")
    if not isinstance(chat, dict) or "id" not in chat:
        raise TelegramLinkingError("Telegram update message is missing chat.id.")

    raw_chat_id = str(chat["id"]).strip()
    if not TELEGRAM_CHAT_ID_PATTERN.match(raw_chat_id):
        raise TelegramLinkingError(f"Invalid Telegram chat ID format: {raw_chat_id!r}")

    # Extract optional start parameter from text: "/start <param>"
    start_parameter = None
    text = message.get("text")
    if isinstance(text, str):
        parts = text.strip().split()
        if len(parts) >= 2 and parts[0].lower() in ("/start", "start"):
            start_parameter = parts[1].strip()

    return raw_chat_id, start_parameter


def verify_and_consume_telegram_link(
    raw_code: str,
    update_payload: dict[str, Any],
) -> dict[str, Any]:
    """Atomically verify and consume a Telegram linking challenge from an inbound update.

    Binds the trusted chat ID to the challenge owner and enables Telegram delivery.
    """
    if not isinstance(raw_code, str) or not raw_code.strip():
        raise TelegramLinkingError("Linking code is required.")

    # Extract chat_id exclusively from verified inbound Telegram update
    chat_id, _ = parse_telegram_update(update_payload)
    code_hash = _hash_code(raw_code)
    now_ts = _now()

    connection = get_db_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")

        row = connection.execute(
            """SELECT challenge_id, owner_type, user_id, guest_identity_id,
                      expires_at, consumed_at
               FROM telegram_link_challenges
               WHERE challenge_code_hash = ?""",
            (code_hash,),
        ).fetchone()

        if row is None:
            connection.rollback()
            raise TelegramLinkingError("Invalid linking code.")

        challenge_id, owner_type, user_id, guest_identity_id, expires_at, consumed_at = row

        if consumed_at is not None:
            connection.rollback()
            raise TelegramLinkingError("This linking challenge has already been used.")

        if expires_at <= now_ts:
            connection.rollback()
            raise TelegramLinkingError("This linking challenge has expired.")

        # Mark challenge consumed atomically
        cursor = connection.execute(
            """UPDATE telegram_link_challenges
               SET consumed_at = ?, telegram_chat_id = ?
               WHERE challenge_id = ? AND consumed_at IS NULL""",
            (now_ts, chat_id, challenge_id),
        )
        if cursor.rowcount == 0:
            connection.rollback()
            raise TelegramLinkingError("Challenge was concurrently consumed.")

        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()

    # Now bind preference in the respective preference repository
    if owner_type == "registered":
        save_channel_preference(
            user_id=user_id,
            channel="telegram",
            recipient=chat_id,
            enabled=True,
            verified_at=now_ts,
        )
        owner_ref = user_id
    else:
        save_guest_channel_preference(
            guest_identity_id=guest_identity_id,
            channel="telegram",
            recipient=chat_id,
            enabled=True,
            verified_at=now_ts,
            consent_at=now_ts,
        )
        owner_ref = guest_identity_id

    return {
        "status": "linked",
        "owner_type": owner_type,
        "owner_id": owner_ref,
        "chat_id": chat_id,
        "verified_at": now_ts,
    }
