"""Repository for durable guest notification identities, sessions, and subscriptions.

Enforces independent guest identity and session management, separate from
ephemeral browser conversation sessions, with audit-ready consent tracking.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from database.db_connection import get_db_connection


SUPPORTED_CHANNELS = ("telegram", "email", "sms", "whatsapp")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_channel(channel: str) -> str:
    if not isinstance(channel, str):
        raise ValueError("Channel must be a string.")
    normalized = channel.strip().lower()
    if normalized not in SUPPORTED_CHANNELS:
        raise ValueError(f"Unsupported notification channel: {channel!r}")
    return normalized


# ---------------------------------------------------------------------------
# Guest Notification Identities
# ---------------------------------------------------------------------------


def create_guest_notification_identity(
    notes: str | None = None,
    guest_identity_id: str | None = None,
) -> str:
    """Create a durable guest notification identity."""
    gid = guest_identity_id or str(uuid.uuid4())
    now_ts = _now()

    connection = get_db_connection()
    try:
        connection.execute(
            """INSERT INTO guest_notification_identities (
                guest_identity_id, created_at, revoked_at, notes
            ) VALUES (?, ?, NULL, ?)""",
            (gid, now_ts, notes),
        )
        connection.commit()
        return gid
    finally:
        connection.close()


def get_guest_notification_identity(guest_identity_id: str) -> dict[str, Any] | None:
    """Retrieve a guest identity record."""
    if not isinstance(guest_identity_id, str) or not guest_identity_id.strip():
        raise ValueError("guest_identity_id must be a non-empty string.")

    connection = get_db_connection()
    try:
        row = connection.execute(
            """SELECT guest_identity_id, created_at, revoked_at, notes
               FROM guest_notification_identities
               WHERE guest_identity_id = ?""",
            (guest_identity_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "guest_identity_id": row[0],
            "created_at": row[1],
            "revoked_at": row[2],
            "notes": row[3],
        }
    finally:
        connection.close()


def revoke_guest_notification_identity(guest_identity_id: str) -> bool:
    """Revoke a guest notification identity and all its active sessions."""
    if not isinstance(guest_identity_id, str) or not guest_identity_id.strip():
        raise ValueError("guest_identity_id must be a non-empty string.")

    now_ts = _now()
    connection = get_db_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """UPDATE guest_notification_identities
               SET revoked_at = ?
               WHERE guest_identity_id = ? AND revoked_at IS NULL""",
            (now_ts, guest_identity_id),
        )
        revoked = cursor.rowcount > 0
        if revoked:
            # Also revoke all associated guest sessions
            connection.execute(
                """UPDATE guest_notification_sessions
                   SET revoked_at = ?
                   WHERE guest_identity_id = ? AND revoked_at IS NULL""",
                (now_ts, guest_identity_id),
            )
        connection.commit()
        return revoked
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Guest Notification Sessions
# ---------------------------------------------------------------------------


def create_guest_session(token_hash: str, guest_identity_id: str, expires_at: str) -> None:
    """Create an independent opaque session for guest notification management."""
    if not isinstance(token_hash, str) or len(token_hash) != 64:
        raise ValueError("token_hash must be a 64-character SHA-256 hex string.")
    if not isinstance(guest_identity_id, str) or not guest_identity_id.strip():
        raise ValueError("guest_identity_id must be a non-empty string.")
    if not isinstance(expires_at, str) or not expires_at.strip():
        raise ValueError("expires_at must be a non-empty ISO timestamp.")

    now_ts = _now()
    connection = get_db_connection()
    try:
        connection.execute(
            """INSERT INTO guest_notification_sessions (
                token_hash, guest_identity_id, created_at, last_active_at, expires_at, revoked_at
            ) VALUES (?, ?, ?, ?, ?, NULL)""",
            (token_hash, guest_identity_id, now_ts, now_ts, expires_at),
        )
        connection.commit()
    finally:
        connection.close()


def get_guest_session(token_hash: str) -> dict[str, Any] | None:
    """Validate a guest session token hash.

    Verifies session is active, unexpired, and associated guest identity is unrevoked.
    Refreshes last_active_at on success.
    """
    if not isinstance(token_hash, str) or len(token_hash) != 64:
        return None

    now_ts = _now()
    connection = get_db_connection()
    try:
        row = connection.execute(
            """SELECT s.token_hash, s.guest_identity_id, s.created_at,
                      s.last_active_at, s.expires_at, s.revoked_at,
                      g.revoked_at AS identity_revoked_at
               FROM guest_notification_sessions s
               JOIN guest_notification_identities g
                 ON g.guest_identity_id = s.guest_identity_id
               WHERE s.token_hash = ?""",
            (token_hash,),
        ).fetchone()

        if row is None:
            return None

        # Check session revocation, identity revocation, and expiration
        session_revoked = row[5]
        identity_revoked = row[6]
        expires_at = row[4]

        if session_revoked is not None or identity_revoked is not None or expires_at <= now_ts:
            return None

        # Refresh last_active_at
        connection.execute(
            "UPDATE guest_notification_sessions SET last_active_at = ? WHERE token_hash = ?",
            (now_ts, token_hash),
        )
        connection.commit()

        return {
            "token_hash": row[0],
            "guest_identity_id": row[1],
            "created_at": row[2],
            "last_active_at": now_ts,
            "expires_at": expires_at,
            "revoked_at": None,
        }
    finally:
        connection.close()


def revoke_guest_session(token_hash: str) -> bool:
    """Revoke a specific guest notification session."""
    if not isinstance(token_hash, str) or len(token_hash) != 64:
        return False

    now_ts = _now()
    connection = get_db_connection()
    try:
        cursor = connection.execute(
            """UPDATE guest_notification_sessions
               SET revoked_at = ?
               WHERE token_hash = ? AND revoked_at IS NULL""",
            (now_ts, token_hash),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def revoke_all_guest_sessions(guest_identity_id: str) -> int:
    """Revoke all active sessions for a guest identity."""
    if not isinstance(guest_identity_id, str) or not guest_identity_id.strip():
        raise ValueError("guest_identity_id must be a non-empty string.")

    now_ts = _now()
    connection = get_db_connection()
    try:
        cursor = connection.execute(
            """UPDATE guest_notification_sessions
               SET revoked_at = ?
               WHERE guest_identity_id = ? AND revoked_at IS NULL""",
            (now_ts, guest_identity_id),
        )
        connection.commit()
        return cursor.rowcount
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Guest Shipment Subscriptions
# ---------------------------------------------------------------------------


def create_guest_shipment_subscription(
    guest_identity_id: str,
    shipment_id: int,
    monitoring_interval_hours: int = 6,
) -> int:
    """Subscribe a guest identity to shipment tracking."""
    if not isinstance(guest_identity_id, str) or not guest_identity_id.strip():
        raise ValueError("guest_identity_id must be a non-empty string.")
    if not isinstance(shipment_id, int) or shipment_id <= 0:
        raise ValueError("shipment_id must be a positive integer.")
    if not isinstance(monitoring_interval_hours, int) or monitoring_interval_hours <= 0:
        raise ValueError("monitoring_interval_hours must be a positive integer.")

    now_ts = _now()
    connection = get_db_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO guest_shipment_subscriptions (
                guest_identity_id, shipment_id, monitoring_enabled,
                monitoring_interval_hours, next_check_at, monitoring_completed_at,
                created_at, updated_at
            ) VALUES (?, ?, 1, ?, ?, NULL, ?, ?)
            ON CONFLICT(guest_identity_id, shipment_id) DO UPDATE SET
                monitoring_enabled = 1,
                monitoring_completed_at = NULL,
                updated_at = excluded.updated_at""",
            (guest_identity_id, shipment_id, monitoring_interval_hours, now_ts, now_ts, now_ts),
        )
        row = connection.execute(
            """SELECT guest_subscription_id FROM guest_shipment_subscriptions
               WHERE guest_identity_id = ? AND shipment_id = ?""",
            (guest_identity_id, shipment_id),
        ).fetchone()
        sub_id = row[0]

        has_monitoring = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='monitoring_jobs'"
        ).fetchone() is not None
        if has_monitoring:
            from database.monitoring_job_repository import _sync_monitoring_job_in_transaction
            _sync_monitoring_job_in_transaction(connection, shipment_id)

        connection.commit()
        return sub_id
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def get_guest_shipment_subscription(guest_identity_id: str, shipment_id: int) -> dict[str, Any] | None:
    """Retrieve a guest subscription for a specific shipment."""
    connection = get_db_connection()
    try:
        row = connection.execute(
            """SELECT guest_subscription_id, guest_identity_id, shipment_id,
                      monitoring_enabled, monitoring_interval_hours, next_check_at,
                      monitoring_completed_at, created_at, updated_at
               FROM guest_shipment_subscriptions
               WHERE guest_identity_id = ? AND shipment_id = ?""",
            (guest_identity_id, shipment_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "guest_subscription_id": row[0],
            "guest_identity_id": row[1],
            "shipment_id": row[2],
            "monitoring_enabled": bool(row[3]),
            "monitoring_interval_hours": row[4],
            "next_check_at": row[5],
            "monitoring_completed_at": row[6],
            "created_at": row[7],
            "updated_at": row[8],
        }
    finally:
        connection.close()


def get_guest_subscription_by_id(guest_subscription_id: int) -> dict[str, Any] | None:
    """Retrieve a guest subscription by its primary key."""
    if not isinstance(guest_subscription_id, int) or guest_subscription_id <= 0:
        raise ValueError("guest_subscription_id must be a positive integer.")

    connection = get_db_connection()
    try:
        row = connection.execute(
            """SELECT guest_subscription_id, guest_identity_id, shipment_id,
                      monitoring_enabled, monitoring_interval_hours, next_check_at,
                      monitoring_completed_at, created_at, updated_at
               FROM guest_shipment_subscriptions
               WHERE guest_subscription_id = ?""",
            (guest_subscription_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "guest_subscription_id": row[0],
            "guest_identity_id": row[1],
            "shipment_id": row[2],
            "monitoring_enabled": bool(row[3]),
            "monitoring_interval_hours": row[4],
            "next_check_at": row[5],
            "monitoring_completed_at": row[6],
            "created_at": row[7],
            "updated_at": row[8],
        }
    finally:
        connection.close()


def update_guest_monitoring_status(guest_subscription_id: int, monitoring_enabled: bool) -> bool:
    """Update active monitoring flag for a guest subscription."""
    if not isinstance(guest_subscription_id, int) or guest_subscription_id <= 0:
        raise ValueError("guest_subscription_id must be a positive integer.")

    now_ts = _now()
    flag = 1 if monitoring_enabled else 0
    connection = get_db_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT shipment_id FROM guest_shipment_subscriptions WHERE guest_subscription_id = ?",
            (guest_subscription_id,),
        ).fetchone()
        if row is None:
            connection.rollback()
            return False
        shipment_id = row[0]

        cursor = connection.execute(
            """UPDATE guest_shipment_subscriptions
               SET monitoring_enabled = ?, updated_at = ?
               WHERE guest_subscription_id = ?""",
            (flag, now_ts, guest_subscription_id),
        )

        has_monitoring = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='monitoring_jobs'"
        ).fetchone() is not None
        if has_monitoring:
            from database.monitoring_job_repository import _sync_monitoring_job_in_transaction
            _sync_monitoring_job_in_transaction(connection, shipment_id)

        connection.commit()
        return cursor.rowcount > 0
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def list_guest_subscriptions(guest_identity_id: str) -> list[dict[str, Any]]:
    """List all shipment subscriptions for a guest identity."""
    connection = get_db_connection()
    try:
        rows = connection.execute(
            """SELECT gs.guest_subscription_id, gs.guest_identity_id, gs.shipment_id,
                      gs.monitoring_enabled, gs.monitoring_interval_hours, gs.next_check_at,
                      gs.monitoring_completed_at, gs.created_at, gs.updated_at,
                      s.tracking_number, s.carrier, s.current_status
               FROM guest_shipment_subscriptions gs
               JOIN shipments s ON s.shipment_id = gs.shipment_id
               WHERE gs.guest_identity_id = ?
               ORDER BY gs.created_at DESC""",
            (guest_identity_id,),
        ).fetchall()
        return [
            {
                "guest_subscription_id": r[0],
                "guest_identity_id": r[1],
                "shipment_id": r[2],
                "monitoring_enabled": bool(r[3]),
                "monitoring_interval_hours": r[4],
                "next_check_at": r[5],
                "monitoring_completed_at": r[6],
                "created_at": r[7],
                "updated_at": r[8],
                "tracking_number": r[9],
                "carrier": r[10],
                "current_status": r[11],
            }
            for r in rows
        ]
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Guest Channel Preferences & Consent
# ---------------------------------------------------------------------------


def save_guest_channel_preference(
    guest_identity_id: str,
    channel: str,
    recipient: str | None,
    enabled: bool,
    verified_at: str | None = None,
    consent_at: str | None = None,
    consent_revoked_at: str | None = None,
) -> int:
    """Save or update a guest's notification channel preference with explicit consent."""
    if not isinstance(guest_identity_id, str) or not guest_identity_id.strip():
        raise ValueError("guest_identity_id must be a non-empty string.")
    norm_channel = _validate_channel(channel)

    norm_recipient = recipient.strip() if isinstance(recipient, str) and recipient.strip() else None
    flag = 1 if enabled else 0
    now_ts = _now()

    connection = get_db_connection()
    try:
        connection.execute(
            """INSERT INTO guest_notification_channel_preferences (
                guest_identity_id, channel, recipient, enabled,
                verified_at, consent_at, consent_revoked_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guest_identity_id, channel) DO UPDATE SET
                recipient = excluded.recipient,
                enabled = excluded.enabled,
                verified_at = COALESCE(excluded.verified_at, guest_notification_channel_preferences.verified_at),
                consent_at = COALESCE(excluded.consent_at, guest_notification_channel_preferences.consent_at),
                consent_revoked_at = excluded.consent_revoked_at,
                updated_at = excluded.updated_at""",
            (
                guest_identity_id, norm_channel, norm_recipient, flag,
                verified_at, consent_at, consent_revoked_at, now_ts, now_ts,
            ),
        )
        connection.commit()
        row = connection.execute(
            """SELECT preference_id FROM guest_notification_channel_preferences
               WHERE guest_identity_id = ? AND channel = ?""",
            (guest_identity_id, norm_channel),
        ).fetchone()
        return row[0]
    finally:
        connection.close()


def get_guest_channel_preference(guest_identity_id: str, channel: str) -> dict[str, Any] | None:
    """Retrieve a guest's preference for a specific channel."""
    norm_channel = _validate_channel(channel)
    connection = get_db_connection()
    try:
        row = connection.execute(
            """SELECT preference_id, guest_identity_id, channel, recipient,
                      enabled, verified_at, consent_at, consent_revoked_at,
                      created_at, updated_at
               FROM guest_notification_channel_preferences
               WHERE guest_identity_id = ? AND channel = ?""",
            (guest_identity_id, norm_channel),
        ).fetchone()
        if row is None:
            return None
        return {
            "preference_id": row[0],
            "guest_identity_id": row[1],
            "channel": row[2],
            "recipient": row[3],
            "enabled": bool(row[4]),
            "verified_at": row[5],
            "consent_at": row[6],
            "consent_revoked_at": row[7],
            "created_at": row[8],
            "updated_at": row[9],
        }
    finally:
        connection.close()


def revoke_guest_channel_consent(guest_identity_id: str, channel: str) -> bool:
    """Revoke consent for a guest channel; immediately disables delivery."""
    norm_channel = _validate_channel(channel)
    now_ts = _now()
    connection = get_db_connection()
    try:
        cursor = connection.execute(
            """UPDATE guest_notification_channel_preferences
               SET enabled = 0, consent_revoked_at = ?, updated_at = ?
               WHERE guest_identity_id = ? AND channel = ?""",
            (now_ts, now_ts, guest_identity_id, norm_channel),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def get_eligible_guest_recipients(guest_identity_id: str) -> list[dict[str, Any]]:
    """Retrieve enabled, verified, and unrevoked channel preferences for a guest."""
    connection = get_db_connection()
    try:
        rows = connection.execute(
            """SELECT channel, recipient, verified_at, consent_at
               FROM guest_notification_channel_preferences
               WHERE guest_identity_id = ?
                 AND enabled = 1
                 AND recipient IS NOT NULL AND TRIM(recipient) <> ''
                 AND verified_at IS NOT NULL AND TRIM(verified_at) <> ''
                 AND consent_revoked_at IS NULL
               ORDER BY channel""",
            (guest_identity_id,),
        ).fetchall()
        return [
            {
                "channel": r[0],
                "recipient": r[1],
                "verified_at": r[2],
                "consent_at": r[3],
            }
            for r in rows
        ]
    finally:
        connection.close()
