"""Channel-independent notification preferences and recipient eligibility."""

import re
from datetime import datetime, timezone

from database.db_connection import get_db_connection

SUPPORTED_CHANNELS = frozenset({"telegram", "email", "sms", "whatsapp"})


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_channel(channel):
    if not isinstance(channel, str) or channel not in SUPPORTED_CHANNELS:
        raise ValueError("Unsupported notification channel.")


def _validate_user_id(user_id):
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("A positive registered user ID is required.")


def _normalize_recipient(channel, recipient):
    if recipient is None:
        return None
    if not isinstance(recipient, str):
        raise ValueError("Recipient must be a string or None.")
    recipient = recipient.strip()
    if not recipient:
        return None
    if len(recipient) > 320 or any(ord(char) < 32 for char in recipient):
        raise ValueError("Recipient has an invalid length or control characters.")
    if channel == "email":
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", recipient):
            raise ValueError("Email recipient is not in a valid basic format.")
    elif channel in {"sms", "whatsapp"}:
        if not re.fullmatch(r"\+[1-9][0-9]{7,14}", recipient):
            raise ValueError("Phone recipient must use international E.164 format.")
    elif channel == "telegram":
        # Telegram numeric chat identifiers can be negative for groups.
        if not re.fullmatch(r"-?[1-9][0-9]{0,19}", recipient):
            raise ValueError("Telegram recipient must be a numeric chat ID.")
    return recipient


def save_channel_preference(
    user_id, channel, recipient=None, enabled=False, verified_at=None
):
    """Create/update one user's channel preference; never stores provider secrets."""
    _validate_user_id(user_id)
    _validate_channel(channel)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean.")
    normalized_recipient = _normalize_recipient(channel, recipient)
    if verified_at is not None:
        if not isinstance(verified_at, str) or not verified_at.strip():
            raise ValueError("verified_at must be a non-empty timestamp string or None.")
        try:
            parsed = datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("verified_at must be an ISO-8601 timestamp.") from exc
        if parsed.tzinfo is None:
            raise ValueError("verified_at must include a timezone.")
        verified_at = parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    if enabled and (not normalized_recipient or not verified_at):
        raise ValueError("An enabled preference requires a recipient and verified_at.")

    now = _now()
    connection = get_db_connection()
    try:
        connection.execute(
            """
            INSERT INTO notification_channel_preferences (
                user_id, channel, recipient, enabled, verified_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, channel) DO UPDATE SET
                recipient = excluded.recipient,
                enabled = excluded.enabled,
                verified_at = excluded.verified_at,
                updated_at = excluded.updated_at
            """,
            (user_id, channel, normalized_recipient, int(enabled), verified_at, now, now),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_channel_preference(user_id, channel):
    _validate_user_id(user_id)
    _validate_channel(channel)
    connection = get_db_connection()
    try:
        cursor = connection.execute(
            """SELECT preference_id, user_id, channel, recipient, enabled,
                      verified_at, created_at, updated_at
               FROM notification_channel_preferences
               WHERE user_id = ? AND channel = ?""",
            (user_id, channel),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [item[0] for item in cursor.description]
        return dict(zip(columns, row))
    finally:
        connection.close()


def list_channel_preferences(user_id, enabled_only=False):
    _validate_user_id(user_id)
    if not isinstance(enabled_only, bool):
        raise ValueError("enabled_only must be a boolean.")
    connection = get_db_connection()
    try:
        sql = """SELECT preference_id, user_id, channel, recipient, enabled,
                        verified_at, created_at, updated_at
                 FROM notification_channel_preferences WHERE user_id = ?"""
        params = [user_id]
        if enabled_only:
            sql += " AND enabled = 1"
        sql += " ORDER BY channel"
        cursor = connection.execute(sql, params)
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        connection.close()


def get_eligible_recipients(user_id):
    """Return only enabled, verified preferences with a usable destination."""
    _validate_user_id(user_id)
    connection = get_db_connection()
    try:
        cursor = connection.execute(
            """SELECT preference_id, user_id, channel, recipient, enabled,
                      verified_at, created_at, updated_at
               FROM notification_channel_preferences
               WHERE user_id = ? AND enabled = 1
                 AND recipient IS NOT NULL AND TRIM(recipient) <> ''
                 AND verified_at IS NOT NULL AND TRIM(verified_at) <> ''
               ORDER BY channel""",
            (user_id,),
        )
        columns = [item[0] for item in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        # Re-validate persisted values; malformed rows must not become destinations.
        eligible = []
        for row in rows:
            try:
                _validate_channel(row["channel"])
                _normalize_recipient(row["channel"], row["recipient"])
                parsed = datetime.fromisoformat(row["verified_at"].replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    continue
            except (ValueError, AttributeError):
                continue
            eligible.append(row)
        return eligible
    finally:
        connection.close()
