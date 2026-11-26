"""Transactional processing of persisted AfterShip webhook events."""

import sqlite3

import json

from datetime import datetime, timezone

from database.db_connection import get_db_connection

from notifications.event_contract import (
    SHIPMENT_STATUS_CHANGED, EVENT_SCHEMA_VERSION,
)
from notifications.generation import (
    generate_notifications_in_transaction,
    generate_notifications_for_change_detection_in_transaction,
)
from notifications.change_detection import detect_and_record_shipment_changes

from database.checkpoint_repository import save_checkpoints_in_transaction

from database.webhook_event_repository import (
    mark_webhook_event_processed_in_transaction,
)

from webhooks.webhook_security import (
    validate_aftership_payload,
)


def _utc_now():
    """Return the current timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def _utc_iso(value=None):
    """Return an ISO-formatted UTC timestamp."""

    if value is None:
        value = _utc_now()

    return value.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    )


def normalize_aftership_webhook_event(payload):
    """
    Normalize a validated AfterShip webhook payload.

    This function is pure:
    - No database access.
    - No external API calls.
    - No OpenAI calls.

    Returns a standardized event dictionary.
    """

    if not isinstance(payload, dict):
        raise ValueError("Webhook payload must be a dictionary.")

    event_type = payload.get("event")
    event_timestamp = payload.get("ts")
    message = payload.get("msg")

    if not isinstance(message, dict):
        raise ValueError("Webhook message must be a dictionary.")

    if event_type not in {
        "tracking_update",
        "edd_revise",
        "tracking_pending_time",
    }:
        raise ValueError("Unsupported webhook event type.")

    if (
        isinstance(event_timestamp, bool)
        or not isinstance(event_timestamp, (int, float))
    ):
        raise ValueError("Invalid provider event timestamp.")

    normalized_checkpoints = []

    raw_checkpoints = message.get("checkpoints") or []

    if not isinstance(raw_checkpoints, list):
        raise ValueError("Webhook checkpoints must be a list.")

    for checkpoint in raw_checkpoints:

        if not isinstance(checkpoint, dict):
            continue

        normalized_checkpoints.append({
            "date": checkpoint.get("checkpoint_time"),
            "location": checkpoint.get("location"),
            "status": checkpoint.get("tag"),
            "message": checkpoint.get("message"),
        })

    return {
        "event_type": event_type,
        "provider_event_timestamp": float(event_timestamp),

        "provider_tracking_id": message.get("id"),

        "tracking_number": message.get("tracking_number"),
        "carrier": message.get("slug"),

        "current_status": message.get("tag"),
        "status_message": message.get("subtag_message"),

        "origin": message.get("origin_raw_location"),
        "destination": message.get("destination_raw_location"),

        "pickup_date": message.get("shipment_pickup_date"),
        "delivery_date": message.get("shipment_delivery_date"),

        "checkpoints": normalized_checkpoints,
    }


def _validate_identifiers(tracking_data):
    """
    Validate shipment identification fields.

    Missing identifiers are permitted. A shipment is created
    or matched only when both tracking_number and carrier
    are available.
    """

    tracking_number = tracking_data.get("tracking_number")
    carrier = tracking_data.get("carrier")

    if tracking_number is not None:
        if (
            not isinstance(tracking_number, str)
            or not tracking_number.strip()
        ):
            raise ValueError("Invalid tracking number.")

    if carrier is not None:
        if (
            not isinstance(carrier, str)
            or not carrier.strip()
        ):
            raise ValueError("Invalid carrier slug.")


def _get_existing_shipment(
    connection,
    tracking_number,
    carrier,
):
    """Retrieve an existing shipment using its unique identity."""

    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT *
        FROM shipments
        WHERE tracking_number = ?
          AND carrier = ?
        """,
        (
            tracking_number,
            carrier,
        ),
    )

    result = cursor.fetchone()

    if result is None:
        return None

    columns = [
        description[0]
        for description in cursor.description
    ]

    return dict(zip(columns, result))


def _create_shipment(
    connection,
    tracking_data,
    provider_event_timestamp,
    current_time,
):
    """
    Create a shipment from an incoming webhook.

    Only tracking_update events establish shipment status.

    No user account, subscription, or notification preference
    is created.
    """

    cursor = connection.cursor()

    is_tracking_update = (
        tracking_data["event_type"] == "tracking_update"
    )

    initial_status = (
        tracking_data.get("current_status")
        if is_tracking_update
        else None
    )

    initial_status_message = (
        tracking_data.get("status_message")
        if is_tracking_update
        else None
    )

    cursor.execute(
        """
        INSERT INTO shipments (
            tracking_number,
            carrier,
            provider,
            provider_tracking_id,
            current_status,
            status_message,
            origin,
            destination,
            pickup_date,
            delivery_date,
            last_checked_at,
            last_status_change_at,
            created_at,
            updated_at,
            last_provider_event_timestamp
        )
        VALUES (
            ?, ?, 'aftership', ?, ?, ?, ?, ?, ?, ?,
            NULL, ?, ?, ?, ?
        )
        """,
        (
            tracking_data["tracking_number"],
            tracking_data["carrier"],
            tracking_data.get("provider_tracking_id"),
            initial_status,
            initial_status_message,
            tracking_data.get("origin"),
            tracking_data.get("destination"),
            tracking_data.get("pickup_date"),
            tracking_data.get("delivery_date"),
            current_time if initial_status is not None else None,
            current_time,
            current_time,
            (
                provider_event_timestamp
                if is_tracking_update
                else None
            ),
        ),
    )

    return cursor.lastrowid


def _update_existing_shipment(
    connection,
    shipment,
    tracking_data,
    current_time,
):
    """
    Update an existing shipment without overwriting fields
    omitted from the incoming webhook.

    Only tracking_update events can update current status.

    Provider timestamp ordering prevents older events from
    overwriting newer shipment information.
    """

    event_type = tracking_data["event_type"]

    if event_type != "tracking_update":
        return False

    event_timestamp = tracking_data[
        "provider_event_timestamp"
    ]

    previous_timestamp = shipment.get(
        "last_provider_event_timestamp"
    )

    if (
        previous_timestamp is not None
        and event_timestamp <= previous_timestamp
    ):
        return False

    cursor = connection.cursor()

    new_status = tracking_data.get("current_status")

    old_status = shipment.get("current_status")

    if new_status is None:
        new_status = old_status

    new_status_message = tracking_data.get("status_message")

    if new_status_message is None:
        new_status_message = shipment.get("status_message")

    new_provider_tracking_id = (
        tracking_data.get("provider_tracking_id")
        or shipment.get("provider_tracking_id")
    )

    new_origin = (
        tracking_data.get("origin")
        if tracking_data.get("origin") is not None
        else shipment.get("origin")
    )

    new_destination = (
        tracking_data.get("destination")
        if tracking_data.get("destination") is not None
        else shipment.get("destination")
    )

    new_pickup_date = (
        tracking_data.get("pickup_date")
        if tracking_data.get("pickup_date") is not None
        else shipment.get("pickup_date")
    )

    new_delivery_date = (
        tracking_data.get("delivery_date")
        if tracking_data.get("delivery_date") is not None
        else shipment.get("delivery_date")
    )

    status_change_time = shipment.get(
        "last_status_change_at"
    )

    if new_status != old_status:
        status_change_time = current_time

    cursor.execute(
        """
        UPDATE shipments

        SET
            provider = 'aftership',
            provider_tracking_id = ?,
            current_status = ?,
            status_message = ?,
            origin = ?,
            destination = ?,
            pickup_date = ?,
            delivery_date = ?,
            last_status_change_at = ?,
            updated_at = ?,
            last_provider_event_timestamp = ?

        WHERE shipment_id = ?
        """,
        (
            new_provider_tracking_id,
            new_status,
            new_status_message,
            new_origin,
            new_destination,
            new_pickup_date,
            new_delivery_date,
            status_change_time,
            current_time,
            event_timestamp,
            shipment["shipment_id"],
        ),
    )

    return True


def process_claimed_webhook_event(
    event_record_id,
    worker_id,
):
    """
    Process an event already claimed by a worker.

    Shipment creation/update, checkpoint insertion and event
    completion occur within one SQLite transaction.

    This function does not make external API calls.

    Returns:
        {
            "event_record_id": str,
            "event_type": str,
            "shipment_id": int or None,
            "shipment_created": bool,
            "status_updated": bool,
            "checkpoints_inserted": int,
            "processed": bool
        }

    Exceptions are propagated to the worker so it can record
    a retryable failure or dead-letter transition.
    """

    if not event_record_id:
        raise ValueError("Event record ID is required.")

    if not isinstance(worker_id, str) or not worker_id.strip():
        raise ValueError("Worker ID is required.")

    connection = get_db_connection()

    try:

        connection.execute("BEGIN IMMEDIATE")

        cursor = connection.cursor()

        cursor.execute(
            """
            SELECT *
            FROM webhook_events
            WHERE event_record_id = ?
              AND status = 'processing'
              AND claimed_by = ?
            """,
            (
                event_record_id,
                worker_id.strip(),
            ),
        )

        event_row = cursor.fetchone()

        if event_row is None:
            raise RuntimeError(
                "Webhook event is not claimed by this worker."
            )

        event_columns = [
            description[0]
            for description in cursor.description
        ]

        event_record = dict(
            zip(event_columns, event_row)
        )

        raw_payload = event_record.get("raw_payload")

        if not isinstance(raw_payload, str):
            raise ValueError("Stored webhook payload is invalid.")

        validated_payload = validate_aftership_payload(
            raw_payload.encode("utf-8")
        )

        tracking_data = normalize_aftership_webhook_event(
            validated_payload
        )

        _validate_identifiers(tracking_data)

        current_time = _utc_iso()

        shipment_id = None
        shipment_created = False
        status_updated = False
        checkpoints_inserted = 0
        previous_status = None
        notification_generation = None
        change_detection = None

        tracking_number = tracking_data.get(
            "tracking_number"
        )

        carrier = tracking_data.get("carrier")

        if tracking_number and carrier:

            shipment = _get_existing_shipment(
                connection,
                tracking_number.strip(),
                carrier.strip().lower(),
            )

            if shipment is None:

                shipment_id = _create_shipment(
                    connection,
                    tracking_data,
                    tracking_data["provider_event_timestamp"],
                    current_time,
                )

                shipment_created = True
                previous_status = None

                status_updated = (
                    tracking_data["event_type"]
                    == "tracking_update"
                )

            else:

                shipment_id = shipment["shipment_id"]
                previous_status = shipment.get("current_status")

                status_updated = _update_existing_shipment(
                    connection,
                    shipment,
                    tracking_data,
                    current_time,
                )

            # Historical checkpoints are independent of the
            # provider event status-ordering watermark.

            checkpoint_save_result = save_checkpoints_in_transaction(
                connection,
                shipment_id,
                tracking_data["checkpoints"],
            )
            checkpoints_inserted = int(checkpoint_save_result)
            newly_inserted_checkpoints = list(
                getattr(checkpoint_save_result, "inserted_checkpoints", ())
            )

            # Migration 7 enables the shared V2 event ledger. Until it is
            # installed, preserve the established V1 webhook notification path.
            ledger_row = None
            if isinstance(connection, sqlite3.Connection):
                ledger_row = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shipment_change_events'"
                ).fetchone()
            change_ledger_available = ledger_row is not None and ledger_row[0] == 1

            # A notification is warranted only for a genuine status transition,
            # not merely an accepted newer provider event or checkpoint update.
            current_row = connection.execute(
                "SELECT current_status, tracking_number, carrier "
                "FROM shipments WHERE shipment_id = ?",
                (shipment_id,),
            ).fetchone()
            current_status = current_row[0] if current_row else None
            status_transition = (
                tracking_data["event_type"] == "tracking_update"
                and current_status is not None
                and current_status != previous_status
            )
            if change_ledger_available:
                status_occurred_at = None
                status_checkpoint_id = None
                if status_transition:
                    status_occurred_at = datetime.fromtimestamp(
                        tracking_data["provider_event_timestamp"], timezone.utc
                    ).isoformat(timespec="microseconds")
                    matching_checkpoint = connection.execute(
                        """SELECT checkpoint_id FROM checkpoints
                           WHERE shipment_id = ? AND LOWER(TRIM(status)) = LOWER(TRIM(?))
                           ORDER BY event_date DESC, checkpoint_id DESC LIMIT 1""",
                        (shipment_id, current_status),
                    ).fetchone()
                    if matching_checkpoint:
                        status_checkpoint_id = matching_checkpoint[0]
                change_detection = detect_and_record_shipment_changes(
                    connection,
                    shipment={
                        "shipment_id": shipment_id,
                        "tracking_number": current_row[1],
                        "carrier": current_row[2],
                    },
                    previous_status=previous_status,
                    new_status=current_status,
                    source="webhook",
                    new_checkpoints=newly_inserted_checkpoints,
                    status_occurred_at=status_occurred_at,
                    recorded_at=current_time,
                    status_checkpoint_id=status_checkpoint_id,
                )
                notification_generation = generate_notifications_for_change_detection_in_transaction(
                    connection, change_detection
                )
            elif status_transition:
                # Backward-compatible V1 behaviour for databases not yet at M7.
                occurred_at = datetime.fromtimestamp(
                    tracking_data["provider_event_timestamp"], timezone.utc
                ).isoformat(timespec="seconds")
                notification_event = {
                    "event_id": event_record_id,
                    "event_type": SHIPMENT_STATUS_CHANGED,
                    "schema_version": EVENT_SCHEMA_VERSION,
                    "source": "aftership_webhook",
                    "occurred_at": occurred_at,
                    "recorded_at": current_time,
                    "shipment": {
                        "shipment_id": shipment_id,
                        "tracking_number": current_row[1],
                        "carrier": current_row[2],
                    },
                    "change": {
                        "previous_status": previous_status,
                        "new_status": current_status,
                    },
                }
                notification_generation = generate_notifications_in_transaction(
                    connection, notification_event
                )

        # Complete the event in the same transaction.

        mark_webhook_event_processed_in_transaction(
            connection,
            event_record_id,
            worker_id.strip(),
        )

        connection.commit()

        return {
            "event_record_id": event_record_id,
            "event_type": tracking_data["event_type"],
            "shipment_id": shipment_id,
            "shipment_created": shipment_created,
            "status_updated": status_updated,
            "checkpoints_inserted": checkpoints_inserted,
            "notification_generation": notification_generation,
            "change_detection": change_detection,
            "processed": True,
        }

    except Exception:

        connection.rollback()

        raise

    finally:

        connection.close()