"""Generate durable, channel-independent shipment notifications."""

import hashlib
from datetime import datetime, timezone

from database.db_connection import get_db_connection
from database.notification_channel_preference_repository import get_eligible_recipients
from database.notification_repository import (
    create_notification, get_notification_by_idempotency_key,
)
from types import SimpleNamespace

from notifications.event_contract import (
    SHIPMENT_STATUS_CHANGED,
    SHIPMENT_CHECKPOINT_ADDED,
    ShipmentChangeEvent,
    parse_shipment_change_event,
)


def _notification_event(event_payload):
    """Normalize V1 status and policy-approved V2 events for generation.

    V1 status notifications remain unchanged. V2 status events use the same
    path; V2 checkpoint events pass through the conservative Step 7 policy.
    """
    parsed = parse_shipment_change_event(event_payload)
    if parsed.schema_version == 1:
        return parsed
    if parsed.event_type == SHIPMENT_CHECKPOINT_ADDED:
        from notifications.policy import evaluate_checkpoint_notification
        decision = evaluate_checkpoint_notification(parsed.change)
        if not decision.notify:
            return None
        change = SimpleNamespace(
            previous_status=None,
            new_status=decision.category,
            checkpoint_id=parsed.change.checkpoint_id,
            checkpoint_location=parsed.change.location,
            checkpoint_message=parsed.change.message,
            checkpoint_event=True,
        )
    elif parsed.event_type == SHIPMENT_STATUS_CHANGED:
        change = SimpleNamespace(
            previous_status=parsed.change.previous_status,
            new_status=parsed.change.new_status,
            checkpoint_id=None,
            checkpoint_location=None,
            checkpoint_message=None,
            checkpoint_event=False,
        )
    else:
        return None
    return SimpleNamespace(
        event_id=parsed.event_id,
        event_type=parsed.event_type,
        schema_version=parsed.schema_version,
        source=parsed.source,
        occurred_at=parsed.occurred_at,
        recorded_at=parsed.recorded_at,
        shipment=parsed.shipment,
        change=change,
    )


def _render_message(event: ShipmentChangeEvent) -> str:
    """Create deterministic text for status or policy-approved checkpoint events."""
    tracking = event.shipment.tracking_number
    carrier = event.shipment.carrier
    if getattr(event.change, "checkpoint_event", False):
        message = (f"Important shipment checkpoint for {tracking} ({carrier}): "
                   f"{event.change.new_status.replace('_', ' ')}.")
    else:
        old = event.change.previous_status or "Unknown"
        new = event.change.new_status
        message = f"Shipment update for {tracking} ({carrier}): {old} → {new}."
    if event.change.checkpoint_location:
        message += f" Location: {event.change.checkpoint_location}."
    if event.change.checkpoint_message:
        message += f" Update: {event.change.checkpoint_message}."
    return message


def _idempotency_key(event_id: str, user_shipment_id: int, channel: str) -> str:
    raw = f"shipment-notification:v1:{event_id}:{user_shipment_id}:{channel}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _guest_idempotency_key(event_id: str, guest_subscription_id: int, channel: str) -> str:
    raw = f"shipment-notification:v1:guest:{event_id}:{guest_subscription_id}:{channel}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_notifications(event_payload):
    """Create one pending notification per subscribed user and eligible channel.

    Supports both registered users and durable guest shipment subscriptions.
    Only active subscriptions with enabled, verified channel preferences
    and unrevoked consent are eligible. This function never delivers.
    """
    event = _notification_event(event_payload)
    if event is None:
        return {
            "event_id": event_payload.get("event_id"),
            "status": "event_not_notifiable",
            "eligible_subscriptions": 0,
            "queued_or_already_queued": 0,
            "skipped_subscriptions": 0,
        }
    if event.change.previous_status == event.change.new_status:
        return {
            "event_id": event.event_id,
            "status": "no_status_change",
            "eligible_subscriptions": 0,
            "queued_or_already_queued": 0,
            "skipped_subscriptions": 0,
        }

    connection = get_db_connection()
    try:
        cursor = connection.execute(
            """SELECT us.user_shipment_id, us.user_id
               FROM user_shipments AS us
               JOIN shipments AS s ON s.shipment_id = us.shipment_id
               WHERE us.shipment_id = ? AND us.monitoring_enabled = 1
                 AND s.tracking_number = ? AND s.carrier = ?
               ORDER BY us.user_shipment_id""",
            (event.shipment.shipment_id, event.shipment.tracking_number,
             event.shipment.carrier),
        )
        subscriptions = [dict(row) for row in _dict_rows(cursor)]

        has_guest_tables = (
            connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='guest_shipment_subscriptions'").fetchone() is not None
            and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='guest_notification_channel_preferences'").fetchone() is not None
        )
        guest_subscriptions = []
        if has_guest_tables:
            g_cursor = connection.execute(
                """SELECT gs.guest_subscription_id, gs.guest_identity_id
                   FROM guest_shipment_subscriptions AS gs
                   JOIN shipments AS s ON s.shipment_id = gs.shipment_id
                   WHERE gs.shipment_id = ? AND gs.monitoring_enabled = 1
                     AND s.tracking_number = ? AND s.carrier = ?
                   ORDER BY gs.guest_subscription_id""",
                (event.shipment.shipment_id, event.shipment.tracking_number,
                 event.shipment.carrier),
            )
            guest_subscriptions = [dict(row) for row in _dict_rows(g_cursor)]
    finally:
        connection.close()

    queued_or_already_queued = 0
    skipped = 0

    # Registered subscriptions
    for subscription in subscriptions:
        recipients = get_eligible_recipients(subscription["user_id"])
        if not recipients:
            skipped += 1
            continue
        for preference in recipients:
            key = _idempotency_key(
                event.event_id, subscription["user_shipment_id"], preference["channel"]
            )
            notification_id = create_notification(
                user_shipment_id=subscription["user_shipment_id"],
                notification_type=preference["channel"],
                recipient=preference["recipient"],
                idempotency_key=key,
                checkpoint_id=event.change.checkpoint_id,
                previous_status=event.change.previous_status,
                new_status=event.change.new_status,
                notification_message=_render_message(event),
            )
            record = get_notification_by_idempotency_key(key)
            if record and record["notification_id"] == notification_id:
                queued_or_already_queued += 1

    # Guest subscriptions
    if guest_subscriptions:
        from database.guest_notification_repository import get_eligible_guest_recipients
        for g_sub in guest_subscriptions:
            g_recipients = get_eligible_guest_recipients(g_sub["guest_identity_id"])
            if not g_recipients:
                skipped += 1
                continue
            for preference in g_recipients:
                key = _guest_idempotency_key(
                    event.event_id, g_sub["guest_subscription_id"], preference["channel"]
                )
                notification_id = create_notification(
                    guest_subscription_id=g_sub["guest_subscription_id"],
                    notification_type=preference["channel"],
                    recipient=preference["recipient"],
                    idempotency_key=key,
                    checkpoint_id=event.change.checkpoint_id,
                    previous_status=event.change.previous_status,
                    new_status=event.change.new_status,
                    notification_message=_render_message(event),
                )
                record = get_notification_by_idempotency_key(key)
                if record and record["notification_id"] == notification_id:
                    queued_or_already_queued += 1

    total_sub_count = len(subscriptions) + len(guest_subscriptions)
    return {
        "event_id": event.event_id,
        "status": "generated",
        "eligible_subscriptions": total_sub_count - skipped,
        "queued_or_already_queued": queued_or_already_queued,
        "skipped_subscriptions": skipped,
    }


def _dict_rows(cursor):
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def generate_notifications_in_transaction(connection, event_payload):
    """Enqueue eligible notifications using the caller's active transaction.

    Unlike :func:`generate_notifications`, this function never opens, commits,
    rolls back, or closes a connection. It is intended for webhook processing,
    where shipment state, webhook completion and notification outbox writes
    must commit or roll back together.
    """
    event = _notification_event(event_payload)
    if event is None:
        return {
            "event_id": event_payload.get("event_id"),
            "status": "event_not_notifiable",
            "eligible_subscriptions": 0,
            "queued_or_already_queued": 0,
            "skipped_subscriptions": 0,
        }
    if event.change.previous_status == event.change.new_status:
        return {
            "event_id": event.event_id,
            "status": "no_status_change",
            "eligible_subscriptions": 0,
            "queued_or_already_queued": 0,
            "skipped_subscriptions": 0,
        }

    required_tables = {"user_shipments", "notification_channel_preferences", "notifications"}
    existing_tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if not required_tables.issubset(existing_tables):
        # Preserve compatibility with minimal/older webhook-only databases.
        return {
            "event_id": event.event_id,
            "status": "notification_schema_unavailable",
            "eligible_subscriptions": 0,
            "queued_or_already_queued": 0,
            "skipped_subscriptions": 0,
        }

    subscriptions = connection.execute(
        """SELECT us.user_shipment_id, us.user_id
           FROM user_shipments AS us
           JOIN shipments AS s ON s.shipment_id = us.shipment_id
           WHERE us.shipment_id = ? AND us.monitoring_enabled = 1
             AND s.tracking_number = ? AND s.carrier = ?
           ORDER BY us.user_shipment_id""",
        (event.shipment.shipment_id, event.shipment.tracking_number,
         event.shipment.carrier),
    ).fetchall()

    queued = 0
    eligible_subscriptions = 0
    skipped = 0
    for user_shipment_id, user_id in subscriptions:
        preferences = connection.execute(
            """SELECT channel, recipient, verified_at
               FROM notification_channel_preferences
               WHERE user_id = ? AND enabled = 1
                 AND recipient IS NOT NULL AND TRIM(recipient) <> ''
                 AND verified_at IS NOT NULL AND TRIM(verified_at) <> ''
               ORDER BY channel""",
            (user_id,),
        ).fetchall()
        valid_preferences = []
        for channel, recipient, verified_at in preferences:
            try:
                from database.notification_channel_preference_repository import (
                    _validate_channel, _normalize_recipient,
                )
                _validate_channel(channel)
                _normalize_recipient(channel, recipient)
                parsed = datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    continue
            except (ValueError, AttributeError, TypeError):
                continue
            valid_preferences.append((channel, recipient))

        if not valid_preferences:
            skipped += 1
            continue

        eligible_subscriptions += 1
        for channel, recipient in valid_preferences:
            key = _idempotency_key(event.event_id, user_shipment_id, channel)
            connection.execute(
                """INSERT INTO notifications (
                       user_shipment_id, checkpoint_id, notification_type,
                       recipient, previous_status, new_status,
                       notification_message, delivery_status, idempotency_key,
                       attempt_count, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, 0, ?)
                   ON CONFLICT(idempotency_key) DO NOTHING""",
                (user_shipment_id, event.change.checkpoint_id, channel, recipient,
                 event.change.previous_status, event.change.new_status,
                 _render_message(event), key,
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            queued += 1

    # Evaluate guest subscriptions if schema supports them
    has_guest_tables = (
        connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='guest_shipment_subscriptions'").fetchone() is not None
        and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='guest_notification_channel_preferences'").fetchone() is not None
    )
    has_guest_col = "guest_subscription_id" in {
        row[1] for row in connection.execute('PRAGMA table_info("notifications")').fetchall()
    }

    if has_guest_tables and has_guest_col:
        guest_subscriptions = connection.execute(
            """SELECT gs.guest_subscription_id, gs.guest_identity_id
               FROM guest_shipment_subscriptions AS gs
               JOIN shipments AS s ON s.shipment_id = gs.shipment_id
               WHERE gs.shipment_id = ? AND gs.monitoring_enabled = 1
                 AND s.tracking_number = ? AND s.carrier = ?
               ORDER BY gs.guest_subscription_id""",
            (event.shipment.shipment_id, event.shipment.tracking_number,
             event.shipment.carrier),
        ).fetchall()

        for guest_subscription_id, guest_identity_id in guest_subscriptions:
            g_preferences = connection.execute(
                """SELECT channel, recipient, verified_at
                   FROM guest_notification_channel_preferences
                   WHERE guest_identity_id = ? AND enabled = 1
                     AND recipient IS NOT NULL AND TRIM(recipient) <> ''
                     AND verified_at IS NOT NULL AND TRIM(verified_at) <> ''
                     AND consent_revoked_at IS NULL
                   ORDER BY channel""",
                (guest_identity_id,),
            ).fetchall()
            valid_g_prefs = []
            for channel, recipient, verified_at in g_preferences:
                try:
                    from database.notification_channel_preference_repository import (
                        _validate_channel, _normalize_recipient,
                    )
                    _validate_channel(channel)
                    _normalize_recipient(channel, recipient)
                    parsed = datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
                    if parsed.tzinfo is None or parsed.utcoffset() is None:
                        continue
                except (ValueError, AttributeError, TypeError):
                    continue
                valid_g_prefs.append((channel, recipient))

            if not valid_g_prefs:
                skipped += 1
                continue

            eligible_subscriptions += 1
            for channel, recipient in valid_g_prefs:
                key = _guest_idempotency_key(event.event_id, guest_subscription_id, channel)
                connection.execute(
                    """INSERT INTO notifications (
                           user_shipment_id, guest_subscription_id, checkpoint_id, notification_type,
                           recipient, previous_status, new_status,
                           notification_message, delivery_status, idempotency_key,
                           attempt_count, created_at
                       ) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, 0, ?)
                       ON CONFLICT(idempotency_key) DO NOTHING""",
                    (guest_subscription_id, event.change.checkpoint_id, channel, recipient,
                     event.change.previous_status, event.change.new_status,
                     _render_message(event), key,
                     datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
                queued += 1

    return {
        "event_id": event.event_id,
        "status": "generated",
        "eligible_subscriptions": eligible_subscriptions,
        "queued_or_already_queued": queued,
        "skipped_subscriptions": skipped,
    }


def generate_notifications_for_change_detection_in_transaction(connection, change_detection):
    """Generate notifications for newly recorded status and checkpoint events.

    The caller owns the transaction. Status notifications retain the legacy
    result shape; checkpoint results are exposed as an additional list when
    present. Suppressed checkpoint events remain in the event ledger and are
    reported as not notifiable by the existing generation function.
    """
    if not isinstance(change_detection, dict):
        raise ValueError("change_detection must be a dictionary.")

    status_event = change_detection.get("status_event")
    checkpoint_events = change_detection.get("checkpoint_events") or []
    if not isinstance(checkpoint_events, (list, tuple)):
        raise ValueError("change_detection.checkpoint_events must be a list or tuple.")

    status_result = None
    if status_event is not None:
        if not isinstance(status_event, dict) or not isinstance(status_event.get("event_payload"), dict):
            raise ValueError("status_event must contain an event_payload dictionary.")
        status_result = generate_notifications_in_transaction(
            connection, status_event["event_payload"]
        )

    checkpoint_results = []
    for index, checkpoint_event in enumerate(checkpoint_events):
        if not isinstance(checkpoint_event, dict) or not isinstance(checkpoint_event.get("event_payload"), dict):
            raise ValueError(
                f"checkpoint_events[{index}] must contain an event_payload dictionary."
            )
        checkpoint_results.append(generate_notifications_in_transaction(
            connection, checkpoint_event["event_payload"]
        ))

    if status_result is None and not checkpoint_results:
        return None
    result = status_result if status_result is not None else dict(checkpoint_results[0])
    if checkpoint_results:
        result = dict(result)
        result["checkpoint_event_results"] = checkpoint_results
        result["checkpoint_events_processed"] = len(checkpoint_results)
        result["checkpoint_notifications_queued_or_existing"] = sum(
            item.get("queued_or_already_queued", 0) for item in checkpoint_results
        )
    return result
