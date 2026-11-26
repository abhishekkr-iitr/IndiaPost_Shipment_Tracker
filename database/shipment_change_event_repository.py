"""Repository for the durable, channel-independent shipment change ledger.

All write operations accept a caller-owned SQLite connection and deliberately
never commit, roll back, or close it. This lets webhook and monitoring callers
include event insertion in their existing shipment/checkpoint transaction.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from notifications.event_contract import (
    EVENT_SCHEMA_VERSION_V2,
    ShipmentChangeEventValidationError,
    parse_shipment_change_event,
)

SUPPORTED_EVENT_TYPES = {
    "shipment.status_changed",
    "shipment.checkpoint_added",
}
SUPPORTED_SOURCES = {"webhook", "monitoring"}
EVENT_SCHEMA_VERSION = EVENT_SCHEMA_VERSION_V2


class ShipmentChangeEventRepositoryError(ValueError):
    """Raised when a shipment change event cannot be persisted safely."""


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ShipmentChangeEventRepositoryError(f"{field} must be a non-empty string.")
    return value.strip()


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ShipmentChangeEventRepositoryError(f"{field} must be a positive integer.")
    return value


def _timestamp(value: Any, field: str) -> str:
    value = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ShipmentChangeEventRepositoryError(
            f"{field} must be a valid ISO 8601 timestamp."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ShipmentChangeEventRepositoryError(
            f"{field} must include a timezone offset."
        )
    return value


def _row_dict(cursor, row):
    if row is None:
        return None
    return dict(zip((item[0] for item in cursor.description), row))


def _load_event_payload(payload: Mapping[str, Any], *, shipment_id: int,
                        event_type: str, source: str, occurred_at: str,
                        recorded_at: str) -> tuple[dict[str, Any], str]:
    """Validate a V2 envelope and its agreement with repository columns."""
    try:
        parsed = parse_shipment_change_event(payload)
    except (ShipmentChangeEventValidationError, TypeError, ValueError) as exc:
        raise ShipmentChangeEventRepositoryError(
            f"event_payload does not satisfy the shipment change event contract: {exc}"
        ) from exc

    if getattr(parsed, "schema_version", None) != EVENT_SCHEMA_VERSION:
        raise ShipmentChangeEventRepositoryError(
            "The change-event ledger accepts only event contract version 2."
        )

    normalized = parsed.to_dict()
    if parsed.event_type != event_type:
        raise ShipmentChangeEventRepositoryError(
            "event_payload.event_type must match event_type."
        )
    if parsed.source != source:
        raise ShipmentChangeEventRepositoryError(
            "event_payload.source must match source."
        )
    if parsed.shipment.shipment_id != shipment_id:
        raise ShipmentChangeEventRepositoryError(
            "event_payload.shipment.shipment_id must match shipment_id."
        )
    if parsed.occurred_at != occurred_at:
        raise ShipmentChangeEventRepositoryError(
            "event_payload.occurred_at must match occurred_at."
        )
    if parsed.recorded_at != recorded_at:
        raise ShipmentChangeEventRepositoryError(
            "event_payload.recorded_at must match recorded_at."
        )
    return normalized, parsed.event_id


def _decode_row(cursor, row):
    result = _row_dict(cursor, row)
    if result is not None:
        result["event_payload"] = json.loads(result["event_payload"])
    return result


def checkpoint_idempotency_key(shipment_id: int, checkpoint_id: int) -> str:
    """Return the cross-source semantic key for one persisted checkpoint."""
    return (
        "shipment-checkpoint:v1:"
        f"{_positive_int(shipment_id, 'shipment_id')}:"
        f"{_positive_int(checkpoint_id, 'checkpoint_id')}"
    )


def record_shipment_change_event(
    connection,
    *,
    shipment_id: int,
    event_type: str,
    source: str,
    idempotency_key: str,
    event_payload: Mapping[str, Any],
    occurred_at: str,
    recorded_at: str | None = None,
    change_event_id: str | None = None,
) -> dict[str, Any]:
    """Insert a V2 event or return a semantically matching existing event.

    The supplied connection is caller-owned. This function never manages
    transaction boundaries. A repeated semantic key may be seen from both
    webhook and monitoring; its original event is returned only when shipment,
    type and semantic payload agree.
    """
    if connection is None or not hasattr(connection, "execute"):
        raise ShipmentChangeEventRepositoryError("A SQLite connection is required.")
    shipment_id = _positive_int(shipment_id, "shipment_id")
    event_type = _required_text(event_type, "event_type")
    if event_type not in SUPPORTED_EVENT_TYPES:
        raise ShipmentChangeEventRepositoryError("Unsupported shipment change event type.")
    source = _required_text(source, "source")
    if source not in SUPPORTED_SOURCES:
        raise ShipmentChangeEventRepositoryError("source must be 'webhook' or 'monitoring'.")
    idempotency_key = _required_text(idempotency_key, "idempotency_key")
    occurred_at = _timestamp(occurred_at, "occurred_at")
    if recorded_at is None:
        recorded_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    recorded_at = _timestamp(recorded_at, "recorded_at")

    if not isinstance(event_payload, Mapping):
        raise ShipmentChangeEventRepositoryError("event_payload must be a mapping.")
    normalized_payload, payload_event_id = _load_event_payload(
        event_payload,
        shipment_id=shipment_id,
        event_type=event_type,
        source=source,
        occurred_at=occurred_at,
        recorded_at=recorded_at,
    )

    if change_event_id is not None:
        change_event_id = _required_text(change_event_id, "change_event_id")
        if change_event_id != payload_event_id:
            raise ShipmentChangeEventRepositoryError(
                "change_event_id must match event_payload.event_id."
            )
    else:
        change_event_id = payload_event_id

    payload_text = json.dumps(
        normalized_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")

    insert_cursor = connection.execute(
        """INSERT INTO shipment_change_events (
            change_event_id, shipment_id, event_type, schema_version, source,
            idempotency_key, event_payload, occurred_at, recorded_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(idempotency_key) DO NOTHING""",
        (
            change_event_id, shipment_id, event_type, EVENT_SCHEMA_VERSION, source,
            idempotency_key, payload_text, occurred_at, recorded_at, created_at,
        ),
    )

    cursor = connection.execute(
        "SELECT * FROM shipment_change_events WHERE idempotency_key = ?",
        (idempotency_key,),
    )
    saved = cursor.fetchone()
    result = _decode_row(cursor, saved)
    if result is None:
        raise RuntimeError(
            "Shipment change event was not inserted or found after idempotency conflict."
        )

    inserted = insert_cursor.rowcount == 1
    # A key collision is only a duplicate if it represents the same semantic
    # change. Source and envelope timestamps may differ across the two systems.
    existing_payload = result["event_payload"]
    if (
        result["shipment_id"] != shipment_id
        or result["event_type"] != event_type
        or result["schema_version"] != EVENT_SCHEMA_VERSION
        or existing_payload.get("shipment") != normalized_payload.get("shipment")
        or existing_payload.get("change") != normalized_payload.get("change")
    ):
        raise ShipmentChangeEventRepositoryError(
            "Idempotency key is already associated with a different shipment change."
        )

    # A pre-existing event can have a different event ID/source/timestamps;
    # return that canonical row for cross-source semantic duplicates.
    # No source equality check: the key intentionally deduplicates webhook
    # and monitoring observations of the same semantic change.
    result["inserted"] = inserted
    return result


def get_shipment_change_event(connection, change_event_id: str):
    change_event_id = _required_text(change_event_id, "change_event_id")
    cursor = connection.execute(
        "SELECT * FROM shipment_change_events WHERE change_event_id = ?",
        (change_event_id,),
    )
    return _decode_row(cursor, cursor.fetchone())


def get_shipment_change_event_by_key(connection, idempotency_key: str):
    idempotency_key = _required_text(idempotency_key, "idempotency_key")
    cursor = connection.execute(
        "SELECT * FROM shipment_change_events WHERE idempotency_key = ?",
        (idempotency_key,),
    )
    return _decode_row(cursor, cursor.fetchone())


def list_shipment_change_events(connection, shipment_id: int, limit: int = 100, offset: int = 0):
    shipment_id = _positive_int(shipment_id, "shipment_id")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ShipmentChangeEventRepositoryError("limit must be an integer from 1 to 500.")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ShipmentChangeEventRepositoryError("offset must be a non-negative integer.")
    cursor = connection.execute(
        """SELECT * FROM shipment_change_events WHERE shipment_id = ?
           ORDER BY occurred_at ASC, recorded_at ASC, change_event_id ASC
           LIMIT ? OFFSET ?""",
        (shipment_id, limit, offset),
    )
    return [_decode_row(cursor, row) for row in cursor.fetchall()]
