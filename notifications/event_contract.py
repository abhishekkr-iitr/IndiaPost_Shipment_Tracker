"""Channel-independent shipment-change event contract.

Version 1 intentionally describes shipment changes only. Recipient selection,
notification preferences, message rendering and delivery channels belong to
separate layers.
"""

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Mapping


EVENT_SCHEMA_VERSION = 1
SHIPMENT_STATUS_CHANGED = "shipment.status_changed"


class ShipmentChangeEventValidationError(ValueError):
    """Raised when a shipment-change event does not meet the contract."""


def _require_non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ShipmentChangeEventValidationError(
            f"{field_name} must be a non-empty string."
        )
    return value.strip()


def _validate_timestamp(value: Any, field_name: str) -> str:
    value = _require_non_empty_string(value, field_name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ShipmentChangeEventValidationError(
            f"{field_name} must be a valid ISO 8601 timestamp."
        ) from exc

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ShipmentChangeEventValidationError(
            f"{field_name} must include a timezone offset."
        )
    return value


def _validate_positive_integer(value: Any, field_name: str):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ShipmentChangeEventValidationError(
            f"{field_name} must be a positive integer."
        )
    return value


def _validate_optional_positive_integer(value: Any, field_name: str):
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ShipmentChangeEventValidationError(
            f"{field_name} must be a positive integer or None."
        )
    return value


@dataclass(frozen=True)
class ShipmentReference:
    shipment_id: int
    tracking_number: str
    carrier: str

    def __post_init__(self):
        _validate_positive_integer(self.shipment_id, "shipment.shipment_id")
        _require_non_empty_string(self.tracking_number, "shipment.tracking_number")
        _require_non_empty_string(self.carrier, "shipment.carrier")


@dataclass(frozen=True)
class ShipmentChange:
    previous_status: str | None
    new_status: str
    checkpoint_id: int | None = None
    checkpoint_event_at: str | None = None
    checkpoint_location: str | None = None
    checkpoint_message: str | None = None

    def __post_init__(self):
        if self.previous_status is not None:
            _require_non_empty_string(self.previous_status, "change.previous_status")
        _require_non_empty_string(self.new_status, "change.new_status")
        _validate_optional_positive_integer(self.checkpoint_id, "change.checkpoint_id")
        if self.checkpoint_event_at is not None:
            _validate_timestamp(self.checkpoint_event_at, "change.checkpoint_event_at")
        for field_name in ("checkpoint_location", "checkpoint_message"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                raise ShipmentChangeEventValidationError(
                    f"change.{field_name} must be a string or None."
                )


@dataclass(frozen=True)
class ShipmentChangeEvent:
    event_id: str
    event_type: str
    schema_version: int
    source: str
    occurred_at: str
    recorded_at: str
    shipment: ShipmentReference
    change: ShipmentChange

    def __post_init__(self):
        _require_non_empty_string(self.event_id, "event_id")
        if self.event_type != SHIPMENT_STATUS_CHANGED:
            raise ShipmentChangeEventValidationError(
                f"event_type must be {SHIPMENT_STATUS_CHANGED!r}."
            )
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != EVENT_SCHEMA_VERSION
        ):
            raise ShipmentChangeEventValidationError(
                f"schema_version must be {EVENT_SCHEMA_VERSION}."
            )
        _require_non_empty_string(self.source, "source")
        _validate_timestamp(self.occurred_at, "occurred_at")
        _validate_timestamp(self.recorded_at, "recorded_at")
        if not isinstance(self.shipment, ShipmentReference):
            raise ShipmentChangeEventValidationError(
                "shipment must be a ShipmentReference."
            )
        if not isinstance(self.change, ShipmentChange):
            raise ShipmentChangeEventValidationError(
                "change must be a ShipmentChange."
            )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary in the version 1 shape."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ShipmentChangeEvent":
        """Validate and construct an event from a mapping."""
        if not isinstance(payload, Mapping):
            raise ShipmentChangeEventValidationError("Event payload must be a mapping.")

        required_top = {
            "event_id", "event_type", "schema_version", "source",
            "occurred_at", "recorded_at", "shipment", "change",
        }
        if set(payload) != required_top:
            missing = sorted(required_top - set(payload))
            extra = sorted(set(payload) - required_top)
            details = []
            if missing:
                details.append(f"missing fields: {', '.join(missing)}")
            if extra:
                details.append(f"unknown fields: {', '.join(extra)}")
            raise ShipmentChangeEventValidationError(
                "Invalid event fields (" + "; ".join(details) + ")."
            )

        shipment = payload["shipment"]
        change = payload["change"]
        if not isinstance(shipment, Mapping):
            raise ShipmentChangeEventValidationError("shipment must be a mapping.")
        if not isinstance(change, Mapping):
            raise ShipmentChangeEventValidationError("change must be a mapping.")

        shipment_fields = {"shipment_id", "tracking_number", "carrier"}
        change_fields = {
            "previous_status", "new_status", "checkpoint_id",
            "checkpoint_event_at", "checkpoint_location", "checkpoint_message",
        }
        if set(shipment) != shipment_fields:
            raise ShipmentChangeEventValidationError(
                "shipment must contain exactly shipment_id, tracking_number and carrier."
            )
        if not {"previous_status", "new_status"}.issubset(change):
            raise ShipmentChangeEventValidationError(
                "change must contain previous_status and new_status."
            )
        if set(change) - change_fields:
            raise ShipmentChangeEventValidationError("change contains unknown fields.")

        return cls(
            event_id=payload["event_id"],
            event_type=payload["event_type"],
            schema_version=payload["schema_version"],
            source=payload["source"],
            occurred_at=payload["occurred_at"],
            recorded_at=payload["recorded_at"],
            shipment=ShipmentReference(**shipment),
            change=ShipmentChange(**change),
        )

# Version 2 adds a shared envelope for status transitions and checkpoint-only
# changes. Version 1 above remains unchanged for existing webhook/Stage 9 users.
EVENT_SCHEMA_VERSION_V2 = 2
SHIPMENT_CHECKPOINT_ADDED = "shipment.checkpoint_added"
EVENT_SOURCE_WEBHOOK = "webhook"
EVENT_SOURCE_MONITORING = "monitoring"


@dataclass(frozen=True)
class ShipmentStatusTransition:
    previous_status: str | None
    new_status: str

    def __post_init__(self):
        if self.previous_status is not None:
            _require_non_empty_string(self.previous_status, "change.previous_status")
        _require_non_empty_string(self.new_status, "change.new_status")


@dataclass(frozen=True)
class ShipmentCheckpointDetails:
    checkpoint_id: int
    checkpoint_event_at: str | None = None
    location: str | None = None
    status: str | None = None
    message: str | None = None

    def __post_init__(self):
        _validate_positive_integer(self.checkpoint_id, "change.checkpoint_id")
        if self.checkpoint_event_at is not None:
            _validate_timestamp(self.checkpoint_event_at, "change.checkpoint_event_at")
        for field_name in ("location", "status", "message"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                raise ShipmentChangeEventValidationError(
                    f"change.{field_name} must be a string or None."
                )


@dataclass(frozen=True)
class ShipmentChangeEventV2:
    """Version 2 event envelope for status and checkpoint changes."""

    event_id: str
    event_type: str
    schema_version: int
    source: str
    occurred_at: str
    recorded_at: str
    shipment: ShipmentReference
    change: ShipmentStatusTransition | ShipmentCheckpointDetails

    def __post_init__(self):
        _require_non_empty_string(self.event_id, "event_id")
        if self.event_type not in {SHIPMENT_STATUS_CHANGED, SHIPMENT_CHECKPOINT_ADDED}:
            raise ShipmentChangeEventValidationError(
                "event_type must be shipment.status_changed or shipment.checkpoint_added."
            )
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != EVENT_SCHEMA_VERSION_V2
        ):
            raise ShipmentChangeEventValidationError(
                f"schema_version must be {EVENT_SCHEMA_VERSION_V2}."
            )
        if self.source not in {EVENT_SOURCE_WEBHOOK, EVENT_SOURCE_MONITORING}:
            raise ShipmentChangeEventValidationError(
                "source must be 'webhook' or 'monitoring'."
            )
        _validate_timestamp(self.occurred_at, "occurred_at")
        _validate_timestamp(self.recorded_at, "recorded_at")
        if not isinstance(self.shipment, ShipmentReference):
            raise ShipmentChangeEventValidationError(
                "shipment must be a ShipmentReference."
            )
        if self.event_type == SHIPMENT_STATUS_CHANGED:
            if not isinstance(self.change, ShipmentStatusTransition):
                raise ShipmentChangeEventValidationError(
                    "status_changed events require ShipmentStatusTransition."
                )
        elif not isinstance(self.change, ShipmentCheckpointDetails):
            raise ShipmentChangeEventValidationError(
                "checkpoint_added events require ShipmentCheckpointDetails."
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ShipmentChangeEventV2":
        if not isinstance(payload, Mapping):
            raise ShipmentChangeEventValidationError("Event payload must be a mapping.")
        required_top = {
            "event_id", "event_type", "schema_version", "source",
            "occurred_at", "recorded_at", "shipment", "change",
        }
        if set(payload) != required_top:
            missing = sorted(required_top - set(payload))
            extra = sorted(set(payload) - required_top)
            details = []
            if missing:
                details.append(f"missing fields: {', '.join(missing)}")
            if extra:
                details.append(f"unknown fields: {', '.join(extra)}")
            raise ShipmentChangeEventValidationError(
                "Invalid event fields (" + "; ".join(details) + ")."
            )
        shipment = payload["shipment"]
        change = payload["change"]
        if not isinstance(shipment, Mapping) or not isinstance(change, Mapping):
            raise ShipmentChangeEventValidationError(
                "shipment and change must be mappings."
            )
        shipment_fields = {"shipment_id", "tracking_number", "carrier"}
        if set(shipment) != shipment_fields:
            raise ShipmentChangeEventValidationError(
                "shipment must contain exactly shipment_id, tracking_number and carrier."
            )

        event_type = payload["event_type"]
        if event_type == SHIPMENT_STATUS_CHANGED:
            if set(change) != {"previous_status", "new_status"}:
                raise ShipmentChangeEventValidationError(
                    "status change must contain exactly previous_status and new_status."
                )
            change_object = ShipmentStatusTransition(**change)
        elif event_type == SHIPMENT_CHECKPOINT_ADDED:
            allowed = {"checkpoint_id", "checkpoint_event_at", "location", "status", "message"}
            if "checkpoint_id" not in change or set(change) - allowed:
                raise ShipmentChangeEventValidationError(
                    "checkpoint change requires checkpoint_id and only supported checkpoint fields."
                )
            change_object = ShipmentCheckpointDetails(**change)
        else:
            raise ShipmentChangeEventValidationError("Unsupported version 2 event_type.")

        return cls(
            event_id=payload["event_id"],
            event_type=event_type,
            schema_version=payload["schema_version"],
            source=payload["source"],
            occurred_at=payload["occurred_at"],
            recorded_at=payload["recorded_at"],
            shipment=ShipmentReference(**shipment),
            change=change_object,
        )


def parse_shipment_change_event(payload: Mapping[str, Any]):
    """Parse either the unchanged version 1 contract or the version 2 contract."""
    if not isinstance(payload, Mapping):
        raise ShipmentChangeEventValidationError("Event payload must be a mapping.")
    version = payload.get("schema_version")
    if version == 1:
        return ShipmentChangeEvent.from_dict(payload)
    if version == EVENT_SCHEMA_VERSION_V2:
        return ShipmentChangeEventV2.from_dict(payload)
    raise ShipmentChangeEventValidationError("Unsupported shipment-change schema version.")
