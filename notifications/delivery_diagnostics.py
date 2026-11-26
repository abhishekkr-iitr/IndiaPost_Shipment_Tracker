"""Read-only delivery diagnostics backed by existing notification records.

No delivery, retry, authorization mutation, schema migration, or provider call
is performed by this module. Returned data intentionally excludes recipients,
message bodies, bot tokens, and arbitrary error text.
"""
from datetime import datetime, timezone
from database.db_connection import get_db_connection


def _utc_now_text():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get_receipt_diagnostics(connection):
    """Return safe receipt event counts and effective states by attempt.

    The event counters describe persisted provider events; the effective
    counters use the shared ordering projection. No recipient, provider event
    payload, provider message ID, or arbitrary provider text is exposed.
    """
    table_exists = connection.execute(
        """SELECT 1 FROM sqlite_master
           WHERE type = 'table' AND name = 'notification_delivery_receipts'"""
    ).fetchone()
    event_counts = {
        "submitted": 0,
        "delivered": 0,
        "failed": 0,
        "read": 0,
    }
    total_events = 0
    unassociated_events = 0
    effective_counts = {
        "submitted": 0,
        "delivered": 0,
        "failed": 0,
        "read": 0,
        "unresolved_or_ambiguous": 0,
    }
    attempts_with_receipts = 0

    if table_exists:
        rows = connection.execute(
            """SELECT receipt_status, COUNT(*)
               FROM notification_delivery_receipts
               GROUP BY receipt_status"""
        ).fetchall()
        for status, count in rows:
            if status in event_counts:
                event_counts[status] = count
            total_events += count

        unassociated_events = connection.execute(
            """SELECT COUNT(*) FROM notification_delivery_receipts
               WHERE attempt_id IS NULL"""
        ).fetchone()[0]

        attempt_rows = connection.execute(
            """SELECT DISTINCT notification_id, attempt_id
               FROM notification_delivery_receipts
               WHERE attempt_id IS NOT NULL
               ORDER BY notification_id, attempt_id"""
        ).fetchall()
        attempts_with_receipts = len(attempt_rows)

        # Reuse the canonical ordering projection rather than implementing a
        # second interpretation of receipt transitions in diagnostics.
        from notifications.receipt_state import get_effective_receipt_state

        for notification_id, attempt_id in attempt_rows:
            projection = get_effective_receipt_state(notification_id, attempt_id)
            status = projection["status"]
            if status in {"submitted", "delivered", "failed", "read"}:
                effective_counts[status] += 1
            else:
                effective_counts["unresolved_or_ambiguous"] += 1

    return {
        "receipt_diagnostics": {
            "total_events": total_events,
            "events_by_status": event_counts,
            "events_without_delivery_attempt": unassociated_events,
            "attempts_with_receipts": attempts_with_receipts,
            "effective_states_by_attempt": effective_counts,
        }
    }


def get_delivery_diagnostics(*, max_attempts=5, now=None):
    """Return aggregate delivery health and safe operational exception counts."""
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts <= 0:
        raise ValueError("max_attempts must be a positive integer.")
    if now is None:
        now_text = _utc_now_text()
    elif isinstance(now, datetime):
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware.")
        now_text = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    else:
        raise TypeError("now must be a timezone-aware datetime or None.")

    connection = get_db_connection()
    try:
        # Enforce read-only behaviour at the SQLite connection level.
        connection.execute("PRAGMA query_only = ON")

        status_rows = connection.execute(
            """SELECT delivery_status, COUNT(*)
               FROM notifications
               GROUP BY delivery_status"""
        ).fetchall()
        by_status = {status: count for status, count in status_rows}

        total = connection.execute(
            "SELECT COUNT(*) FROM notifications"
        ).fetchone()[0]

        retry_due = connection.execute(
            """SELECT COUNT(*) FROM notifications
               WHERE delivery_status = 'pending'
                 AND next_retry_at IS NOT NULL
                 AND next_retry_at <= ?""",
            (now_text,),
        ).fetchone()[0]

        retry_waiting = connection.execute(
            """SELECT COUNT(*) FROM notifications
               WHERE delivery_status = 'pending'
                 AND next_retry_at IS NOT NULL
                 AND next_retry_at > ?""",
            (now_text,),
        ).fetchone()[0]

        attempts_near_limit = connection.execute(
            """SELECT COUNT(*) FROM notifications
               WHERE delivery_status = 'pending'
                 AND attempt_count >= ?""",
            (max(1, max_attempts - 1),),
        ).fetchone()[0]

        expired_leases = connection.execute(
            """SELECT COUNT(*) FROM notifications
               WHERE delivery_status = 'pending'
                 AND claimed_by IS NOT NULL
                 AND lease_expires_at IS NOT NULL
                 AND lease_expires_at <= ?""",
            (now_text,),
        ).fetchone()[0]

        active_leases = connection.execute(
            """SELECT COUNT(*) FROM notifications
               WHERE delivery_status = 'pending'
                 AND claimed_by IS NOT NULL
                 AND lease_expires_at > ?""",
            (now_text,),
        ).fetchone()[0]

        missing_retry_time = connection.execute(
            """SELECT COUNT(*) FROM notifications
               WHERE delivery_status = 'pending'
                 AND attempt_count > 0
                 AND next_retry_at IS NULL
                 AND claimed_by IS NULL""",
        ).fetchone()[0]

        receipt_diagnostics = _get_receipt_diagnostics(connection)

        return {
            "generated_at": now_text,
            **receipt_diagnostics,
            "total_notifications": total,
            "by_status": {
                "pending": by_status.get("pending", 0),
                "sent": by_status.get("sent", 0),
                "failed": by_status.get("failed", 0),
                "other": sum(
                    count for status, count in by_status.items()
                    if status not in {"pending", "sent", "failed"}
                ),
            },
            "pending_retry_due": retry_due,
            "pending_retry_waiting": retry_waiting,
            "pending_at_or_near_attempt_limit": attempts_near_limit,
            "active_worker_leases": active_leases,
            "expired_worker_leases": expired_leases,
            "pending_after_attempt_without_retry_time": missing_retry_time,
        }
    finally:
        connection.close()
