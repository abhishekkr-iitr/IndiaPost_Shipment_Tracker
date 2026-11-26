"""Transactional persistence for normalized provider delivery receipts."""
from datetime import datetime, timezone

from database.db_connection import get_db_connection
from notifications.receipts import ProviderReceipt


def _utc_iso(value):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must be timezone-aware.")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def record_provider_receipt(notification_id, receipt):
    """Persist one normalized receipt idempotently.

    Returns {"recorded": bool, "receipt_id": int | None, "duplicate": bool}.
    This function never dispatches messages or changes delivery retry state.
    """
    if (not isinstance(notification_id, int) or isinstance(notification_id, bool)
            or notification_id <= 0):
        raise ValueError("notification_id must be a positive integer.")
    if not isinstance(receipt, ProviderReceipt):
        raise TypeError("receipt must be a ProviderReceipt.")

    received_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    event_at = _utc_iso(receipt.provider_event_at)
    connection = get_db_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")

        notification = connection.execute(
            "SELECT notification_id FROM notifications WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()
        if notification is None:
            raise ValueError("Receipt references an unknown notification.")

        if receipt.attempt_id is not None:
            attempt = connection.execute(
                """SELECT notification_id, provider, provider_message_id
                   FROM notification_delivery_attempts WHERE attempt_id = ?""",
                (receipt.attempt_id,),
            ).fetchone()
            if attempt is None or attempt[0] != notification_id:
                raise ValueError("Receipt attempt does not belong to this notification.")
            if str(attempt[1]).strip().lower() != receipt.provider.strip().lower():
                raise ValueError("Receipt provider does not match the delivery attempt.")
            if attempt[2] and attempt[2] != receipt.provider_message_id:
                raise ValueError("Receipt message ID does not match the delivery attempt.")

        cursor = connection.execute(
            """INSERT OR IGNORE INTO notification_delivery_receipts (
                   notification_id, attempt_id, provider, provider_event_id,
                   provider_message_id, receipt_status, provider_event_at,
                   received_at, processed_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                notification_id, receipt.attempt_id, receipt.provider.strip().lower(),
                receipt.provider_event_id, receipt.provider_message_id,
                receipt.status, event_at, received_at, received_at,
            ),
        )
        if cursor.rowcount == 0:
            existing = connection.execute(
                """SELECT receipt_id, notification_id, attempt_id,
                          provider_message_id, receipt_status, provider_event_at
                   FROM notification_delivery_receipts
                   WHERE provider = ? AND provider_event_id = ?""",
                (receipt.provider.strip().lower(), receipt.provider_event_id),
            ).fetchone()
            if existing is None:
                raise ValueError("Duplicate provider event could not be retrieved.")
            expected = (
                notification_id, receipt.attempt_id, receipt.provider_message_id,
                receipt.status, event_at,
            )
            actual = tuple(existing[1:])
            if actual != expected:
                raise ValueError(
                    "Duplicate provider event conflicts with existing receipt data."
                )
            connection.commit()
            return {"recorded": False, "receipt_id": existing[0], "duplicate": True}

        receipt_id = cursor.lastrowid
        connection.commit()
        return {"recorded": True, "receipt_id": receipt_id, "duplicate": False}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
