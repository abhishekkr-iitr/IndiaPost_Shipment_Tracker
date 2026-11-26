"""Shared, channel-independent detection of persisted shipment changes.

This module records V2 change events inside a caller-managed SQLite
transaction. It does not fetch tracking data, persist shipments/checkpoints,
commit, roll back, or deliver notifications.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from database.shipment_change_event_repository import (
    checkpoint_idempotency_key,
    record_shipment_change_event,
)
from notifications.event_contract import (
    EVENT_SCHEMA_VERSION_V2,
    SHIPMENT_CHECKPOINT_ADDED,
    SHIPMENT_STATUS_CHANGED,
)


class ShipmentChangeDetectionError(ValueError):
    """Raised when change-detection input is incomplete or inconsistent."""


SUPPORTED_SOURCES = {"webhook", "monitoring"}


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ShipmentChangeDetectionError(f"{field} must be a non-empty string.")
    return value.strip()


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ShipmentChangeDetectionError(f"{field} must be a positive integer.")
    return value


def _parse_aware_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ShipmentChangeDetectionError(f"{field} must be a timezone-aware ISO 8601 timestamp.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ShipmentChangeDetectionError(
            f"{field} must be a valid ISO 8601 timestamp."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ShipmentChangeDetectionError(f"{field} must include a timezone offset.")
    return parsed


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _recorded_timestamp(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")
    return _iso_utc(_parse_aware_timestamp(value, "recorded_at"))


def _shipment_reference(shipment: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(shipment, Mapping):
        raise ShipmentChangeDetectionError("shipment must be a mapping.")
    return {
        "shipment_id": _positive_int(shipment.get("shipment_id"), "shipment.shipment_id"),
        "tracking_number": _required_text(shipment.get("tracking_number"), "shipment.tracking_number"),
        "carrier": _required_text(shipment.get("carrier"), "shipment.carrier"),
    }


def _status_idempotency_key(shipment_id: int, previous_status: str | None,
                            new_status: str, occurred_at: str,
                            checkpoint_id: int | None = None) -> str:
    """Key one status-transition occurrence, not merely a status pair.

    Including the normalized occurrence timestamp allows a later legitimate
    recurrence of the same previous/new status pair to be recorded separately.
    """
    identity = {
        "shipment_id": shipment_id,
        "previous_status": previous_status,
        "new_status": new_status,
        # A persisted checkpoint is the shared cross-source identity when
        # available. Timestamp remains the compatibility fallback.
        "checkpoint_id": checkpoint_id,
        "occurred_at": None if checkpoint_id is not None else occurred_at,
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"shipment-status-transition:v1:{shipment_id}:{digest}"


def detect_and_record_shipment_changes(
    connection,
    *,
    shipment: Mapping[str, Any],
    previous_status: str | None,
    new_status: str | None,
    source: str,
    new_checkpoints: Sequence[Mapping[str, Any]] | None = None,
    status_occurred_at: str | None = None,
    recorded_at: str | None = None,
    status_checkpoint_id: int | None = None,
) -> dict[str, Any]:
    """Detect and record a genuine status transition and newly persisted checkpoints.

    ``new_checkpoints`` must contain only checkpoints newly inserted into the
    canonical ``checkpoints`` table, with their persisted ``checkpoint_id``.
    Existing checkpoints must not be passed again. This is an explicit caller
    contract until Step 6.3.2 supplies repository-level inserted-row details.

    A status transition requires an authoritative occurrence timestamp. The
    detector intentionally does not invent one or fall back to a status pair.
    Checkpoint identity is its persisted checkpoint ID, so duplicate webhook
    and monitoring observations share the same idempotency key.

    The caller owns the transaction. This function never commits, rolls back,
    or closes ``connection``.
    """
    if connection is None or not hasattr(connection, "execute"):
        raise ShipmentChangeDetectionError("A caller-owned SQLite connection is required.")
    if source not in SUPPORTED_SOURCES:
        raise ShipmentChangeDetectionError("source must be 'webhook' or 'monitoring'.")
    source = _required_text(source, "source")
    shipment_ref = _shipment_reference(shipment)
    shipment_id = shipment_ref["shipment_id"]
    recorded_at = _recorded_timestamp(recorded_at)

    if previous_status is not None:
        previous_status = _required_text(previous_status, "previous_status")
    if new_status is not None:
        new_status = _required_text(new_status, "new_status")

    if new_checkpoints is None:
        new_checkpoints = []
    if isinstance(new_checkpoints, (str, bytes)) or not isinstance(new_checkpoints, Sequence):
        raise ShipmentChangeDetectionError("new_checkpoints must be a sequence of mappings.")

    status_event = None
    checkpoint_events = []

    if new_status is not None and new_status != previous_status:
        if status_occurred_at is None:
            raise ShipmentChangeDetectionError(
                "status_occurred_at is required for a status transition; an occurrence timestamp must not be invented."
            )
        occurred_at = _iso_utc(_parse_aware_timestamp(status_occurred_at, "status_occurred_at"))
        event_id = str(uuid.uuid4())
        payload = {
            "event_id": event_id,
            "event_type": SHIPMENT_STATUS_CHANGED,
            "schema_version": EVENT_SCHEMA_VERSION_V2,
            "source": source,
            "occurred_at": occurred_at,
            "recorded_at": recorded_at,
            "shipment": shipment_ref,
            "change": {
                "previous_status": previous_status,
                "new_status": new_status,
            },
        }
        if status_checkpoint_id is not None:
            status_checkpoint_id = _positive_int(status_checkpoint_id, "status_checkpoint_id")
        key = _status_idempotency_key(
            shipment_id, previous_status, new_status, occurred_at, status_checkpoint_id
        )
        status_event = record_shipment_change_event(
            connection,
            shipment_id=shipment_id,
            event_type=SHIPMENT_STATUS_CHANGED,
            source=source,
            idempotency_key=key,
            event_payload=payload,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
        )

    seen_checkpoint_ids = set()
    for index, checkpoint in enumerate(new_checkpoints):
        if not isinstance(checkpoint, Mapping):
            raise ShipmentChangeDetectionError(
                f"new_checkpoints[{index}] must be a mapping."
            )
        checkpoint_id = _positive_int(
            checkpoint.get("checkpoint_id"), f"new_checkpoints[{index}].checkpoint_id"
        )
        if checkpoint_id in seen_checkpoint_ids:
            continue
        seen_checkpoint_ids.add(checkpoint_id)

        checkpoint_event_at = checkpoint.get("event_date")
        if checkpoint_event_at is not None:
            # The existing checkpoint repository treats timezone-naive
            # provider dates as UTC. Match that established normalization.
            if isinstance(checkpoint_event_at, str):
                raw_checkpoint_time = checkpoint_event_at.strip()
                try:
                    parsed_checkpoint_time = datetime.fromisoformat(
                        raw_checkpoint_time.replace("Z", "+00:00")
                    )
                except ValueError as exc:
                    raise ShipmentChangeDetectionError(
                        f"new_checkpoints[{index}].event_date must be a valid ISO 8601 timestamp."
                    ) from exc
                if parsed_checkpoint_time.tzinfo is None:
                    parsed_checkpoint_time = parsed_checkpoint_time.replace(tzinfo=timezone.utc)
                checkpoint_event_at = _iso_utc(parsed_checkpoint_time)
            else:
                raise ShipmentChangeDetectionError(
                    f"new_checkpoints[{index}].event_date must be a timestamp string or None."
                )
            occurred_at = checkpoint_event_at
        else:
            # The true provider occurrence is unknown. occurred_at is the
            # ledger's observation time; checkpoint_event_at remains null.
            occurred_at = recorded_at

        change = {"checkpoint_id": checkpoint_id}
        if checkpoint_event_at is not None:
            change["checkpoint_event_at"] = checkpoint_event_at
        for source_field, target_field in (
            ("location", "location"),
            ("status", "status"),
            ("message", "message"),
        ):
            value = checkpoint.get(source_field)
            if value is not None:
                if not isinstance(value, str):
                    raise ShipmentChangeDetectionError(
                        f"new_checkpoints[{index}].{source_field} must be a string or None."
                    )
                change[target_field] = value

        event_id = str(uuid.uuid4())
        payload = {
            "event_id": event_id,
            "event_type": SHIPMENT_CHECKPOINT_ADDED,
            "schema_version": EVENT_SCHEMA_VERSION_V2,
            "source": source,
            "occurred_at": occurred_at,
            "recorded_at": recorded_at,
            "shipment": shipment_ref,
            "change": change,
        }
        key = checkpoint_idempotency_key(shipment_id, checkpoint_id)
        event = record_shipment_change_event(
            connection,
            shipment_id=shipment_id,
            event_type=SHIPMENT_CHECKPOINT_ADDED,
            source=source,
            idempotency_key=key,
            event_payload=payload,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
        )
        checkpoint_events.append(event)

    return {
        "shipment_id": shipment_id,
        "source": source,
        "status_changed": status_event is not None,
        "status_event": status_event,
        "checkpoint_events": checkpoint_events,
        "checkpoint_events_recorded": len(checkpoint_events),
    }
