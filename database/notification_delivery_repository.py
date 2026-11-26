"""Atomic claiming, current-recipient authorization and outcome persistence."""
from datetime import datetime, timedelta, timezone
from database.db_connection import get_db_connection


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def is_notification_authorized(notification_id):
    """Fail-closed check immediately before dispatch.

    Supports both registered-user subscriptions and guest subscriptions.
    A queued destination is usable only while its shipment subscription remains
    enabled and the matching channel preference remains enabled, verified, unrevoked,
    and unchanged. This prevents sending to a destination after opt-out, revocation,
    or recipient replacement that occurred after notification generation.
    """
    if isinstance(notification_id, bool) or not isinstance(notification_id, int) or notification_id <= 0:
        raise ValueError("notification_id must be a positive integer.")
    connection = get_db_connection()
    try:
        # Check column definitions
        cols = {row[1] for row in connection.execute('PRAGMA table_info("notifications")').fetchall()}
        has_guest_col = "guest_subscription_id" in cols

        if has_guest_col:
            notif_row = connection.execute(
                """SELECT user_shipment_id, guest_subscription_id, recipient, notification_type
                   FROM notifications
                   WHERE notification_id = ?""",
                (notification_id,),
            ).fetchone()
        else:
            legacy_row = connection.execute(
                """SELECT user_shipment_id, recipient, notification_type
                   FROM notifications
                   WHERE notification_id = ?""",
                (notification_id,),
            ).fetchone()
            notif_row = (legacy_row[0], None, legacy_row[1], legacy_row[2]) if legacy_row else None

        if notif_row is None:
            return False

        user_shipment_id, guest_subscription_id, queued_recipient, channel = notif_row

        if not isinstance(queued_recipient, str) or not queued_recipient.strip():
            return False
        if not isinstance(channel, str) or not channel.strip():
            return False

        # Registered user ownership path
        if user_shipment_id is not None and guest_subscription_id is None:
            row = connection.execute(
                """SELECT us.monitoring_enabled,
                          p.recipient AS current_recipient,
                          p.enabled, p.verified_at
                   FROM user_shipments AS us
                   JOIN notification_channel_preferences AS p
                     ON p.user_id = us.user_id
                    AND p.channel = ?
                   WHERE us.user_shipment_id = ?""",
                (channel, user_shipment_id),
            ).fetchone()
            if row is None:
                return False
            monitoring_enabled, current_recipient, enabled, verified_at = row
            if monitoring_enabled != 1 or enabled != 1:
                return False
            if not isinstance(current_recipient, str) or current_recipient.strip() != queued_recipient.strip():
                return False
            if not isinstance(verified_at, str) or not verified_at.strip():
                return False
            try:
                parsed = datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
            except ValueError:
                return False
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return False
            return True

        # Guest user ownership path
        elif guest_subscription_id is not None and user_shipment_id is None:
            has_guest_tables = (
                connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='guest_shipment_subscriptions'").fetchone() is not None
                and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='guest_notification_channel_preferences'").fetchone() is not None
            )
            if not has_guest_tables:
                return False

            row = connection.execute(
                """SELECT gs.monitoring_enabled,
                          gp.recipient AS current_recipient,
                          gp.enabled, gp.verified_at, gp.consent_revoked_at,
                          gi.revoked_at AS identity_revoked_at
                   FROM guest_shipment_subscriptions AS gs
                   JOIN guest_notification_identities AS gi
                     ON gi.guest_identity_id = gs.guest_identity_id
                   JOIN guest_notification_channel_preferences AS gp
                     ON gp.guest_identity_id = gs.guest_identity_id
                    AND gp.channel = ?
                   WHERE gs.guest_subscription_id = ?""",
                (channel, guest_subscription_id),
            ).fetchone()
            if row is None:
                return False
            monitoring_enabled, current_recipient, enabled, verified_at, consent_revoked_at, identity_revoked_at = row
            if identity_revoked_at is not None or consent_revoked_at is not None:
                return False
            if monitoring_enabled != 1 or enabled != 1:
                return False
            if not isinstance(current_recipient, str) or current_recipient.strip() != queued_recipient.strip():
                return False
            if not isinstance(verified_at, str) or not verified_at.strip():
                return False
            try:
                parsed = datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
            except ValueError:
                return False
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return False
            return True

        # Inconsistent or unpopulated ownership -> fail-closed
        return False
    finally:
        connection.close()


def claim_due_notifications(worker_id, limit=20, lease_seconds=120, max_attempts=5):
    """Atomically claim due pending notifications and return claimed rows."""
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise ValueError("worker_id must be a non-empty string.")
    for name, value in (("limit", limit), ("lease_seconds", lease_seconds), ("max_attempts", max_attempts)):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name} must be a positive integer.")
    now = _now()
    now_text = _iso(now)
    lease_text = _iso(now + timedelta(seconds=lease_seconds))
    connection = get_db_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """SELECT notification_id FROM notifications
               WHERE delivery_status = 'pending'
                 AND attempt_count < ?
                 AND (next_retry_at IS NULL OR next_retry_at <= ?)
                 AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
               ORDER BY created_at ASC, notification_id ASC LIMIT ?""",
            (max_attempts, now_text, now_text, limit),
        ).fetchall()
        claimed = []
        for (notification_id,) in rows:
            # A still-open attempt from an expired lease did not reach a
            # durable outcome. Close it before recording the recovered attempt.
            connection.execute(
                """UPDATE notification_delivery_attempts
                   SET outcome = 'interrupted', completed_at = ?,
                       error_message = 'Worker lease expired before attempt completion.'
                   WHERE notification_id = ? AND outcome = 'in_progress'""",
                (now_text, notification_id),
            )
            cursor = connection.execute(
                """UPDATE notifications
                   SET claimed_by = ?, claimed_at = ?, lease_expires_at = ?,
                       attempt_count = attempt_count + 1, last_attempt_at = ?
                   WHERE notification_id = ? AND delivery_status = 'pending'
                     AND (lease_expires_at IS NULL OR lease_expires_at <= ?)""",
                (worker_id, now_text, lease_text, now_text, notification_id, now_text),
            )
            if cursor.rowcount == 1:
                cursor = connection.execute("SELECT * FROM notifications WHERE notification_id = ?", (notification_id,))
                row = cursor.fetchone()
                notification = dict(zip([item[0] for item in cursor.description], row))
                connection.execute(
                    """INSERT INTO notification_delivery_attempts
                       (notification_id, attempt_number, worker_id, channel, provider,
                        started_at, outcome)
                       VALUES (?, ?, ?, ?, ?, ?, 'in_progress')""",
                    (notification_id, notification["attempt_count"], worker_id,
                     notification["notification_type"], notification["notification_type"], now_text),
                )
                claimed.append(notification)
        connection.commit()
        return claimed
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def complete_notification(notification_id, worker_id, provider_message_id):
    return _finish(notification_id, worker_id, "sent", provider_message_id, None, None)


def retry_notification(notification_id, worker_id, error_message, retry_after_seconds):
    if not isinstance(retry_after_seconds, int) or isinstance(retry_after_seconds, bool) or retry_after_seconds < 0:
        raise ValueError("retry_after_seconds must be a non-negative integer.")
    next_retry = _iso(_now() + timedelta(seconds=retry_after_seconds))
    return _finish(notification_id, worker_id, "pending", None, error_message, next_retry)


def fail_notification(notification_id, worker_id, error_message):
    return _finish(notification_id, worker_id, "failed", None, error_message, None)


def _finish(notification_id, worker_id, status, provider_message_id, error_message, next_retry_at):
    if not notification_id or not worker_id:
        raise ValueError("notification_id and worker_id are required.")
    connection = get_db_connection()
    try:
        cursor = connection.execute(
            """UPDATE notifications SET delivery_status = ?,
                   provider_message_id = COALESCE(?, provider_message_id),
                   sent_at = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END,
                   error_message = ?, next_retry_at = ?,
                   claimed_by = NULL, claimed_at = NULL, lease_expires_at = NULL
               WHERE notification_id = ? AND claimed_by = ?
                 AND delivery_status = 'pending'""",
            (status, provider_message_id, status, _iso(_now()), error_message,
             next_retry_at, notification_id, worker_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Notification is not actively claimed by this worker.")
        attempt_outcome = "sent" if status == "sent" else (
            "retry_scheduled" if status == "pending" else "failed"
        )
        attempt_cursor = connection.execute(
            """UPDATE notification_delivery_attempts
               SET outcome = ?, completed_at = ?,
                   provider_message_id = COALESCE(?, provider_message_id),
                   error_message = ?
               WHERE notification_id = ? AND attempt_number = ?
                 AND worker_id = ? AND outcome = 'in_progress'""",
            (attempt_outcome, _iso(_now()), provider_message_id, error_message,
             notification_id,
             connection.execute(
                 "SELECT attempt_count FROM notifications WHERE notification_id = ?",
                 (notification_id,),
             ).fetchone()[0],
             worker_id),
        )
        if attempt_cursor.rowcount != 1:
            raise ValueError("Active delivery attempt history record is missing.")
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def fail_exhausted_notifications(max_attempts=5):
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts <= 0:
        raise ValueError("max_attempts must be a positive integer.")
    now = _iso(_now())
    connection = get_db_connection()
    try:
        cursor = connection.execute(
            """UPDATE notifications SET delivery_status = 'failed',
                   error_message = COALESCE(error_message, 'Maximum delivery attempts reached.'),
                   next_retry_at = NULL,
                   claimed_by = NULL, claimed_at = NULL, lease_expires_at = NULL
               WHERE delivery_status = 'pending' AND attempt_count >= ?
                 AND (lease_expires_at IS NULL OR lease_expires_at <= ?)""",
            (max_attempts, now),
        )
        connection.commit()
        return cursor.rowcount
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
