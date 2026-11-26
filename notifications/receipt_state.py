"""Read-only, deterministic receipt-state projection with out-of-order safeguards.

All provider receipt events remain in the append-only receipt history. This
module derives the effective state for one delivery attempt without changing
notification status, delivery-attempt outcomes, or retry scheduling.
"""
from datetime import datetime
from itertools import permutations

from database.db_connection import get_db_connection


_ALLOWED_TRANSITIONS = {
    None: frozenset({"submitted", "delivered", "failed", "read"}),
    "submitted": frozenset({"submitted", "delivered", "failed"}),
    "delivered": frozenset({"delivered", "read"}),
    "failed": frozenset({"failed"}),
    "read": frozenset({"read"}),
}


def _parse_event_time(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Stored receipt event timestamp is invalid.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Stored receipt event timestamp must be timezone-aware.")
    return parsed


def _apply_status(current, incoming):
    if incoming not in _ALLOWED_TRANSITIONS[current]:
        return None, False
    return incoming, True


def _resolve_timestamp_group(current, events):
    """Resolve simultaneous events only when their valid ordering is unique."""
    statuses = [event["status"] for event in events]
    if len(statuses) == 1:
        next_state, valid = _apply_status(current, statuses[0])
        return next_state, (not valid), False

    outcomes = set()
    valid_orders = 0
    # There are only four supported receipt statuses, so exhaustive ordering
    # is bounded and makes same-timestamp conflict handling deterministic.
    for ordering in set(permutations(statuses)):
        state = current
        valid = True
        for status in ordering:
            state, accepted = _apply_status(state, status)
            if not accepted:
                valid = False
                break
        if valid:
            valid_orders += 1
            outcomes.add(state)

    if valid_orders == 0:
        return current, False, True
    if len(outcomes) != 1:
        return current, False, True
    return outcomes.pop(), False, False


def get_effective_receipt_state(notification_id, attempt_id):
    """Return the effective receipt state for one notification delivery attempt.

    Events are ordered by provider_event_at, not local arrival/receipt_id.
    Older events are retained in history but cannot roll state backwards.
    Invalid later transitions are counted and ignored. Conflicting events with
    the same provider timestamp are reported as ambiguous and do not advance
    the projection.

    This is a read-only projection. It never writes to the database or triggers
    delivery, retries, notifications, or provider calls.
    """
    for name, value in (("notification_id", notification_id), ("attempt_id", attempt_id)):
        if (not isinstance(value, int) or isinstance(value, bool) or value <= 0):
            raise ValueError(f"{name} must be a positive integer.")

    connection = get_db_connection()
    try:
        attempt = connection.execute(
            """SELECT notification_id FROM notification_delivery_attempts
               WHERE attempt_id = ?""",
            (attempt_id,),
        ).fetchone()
        if attempt is None or attempt[0] != notification_id:
            raise ValueError("Delivery attempt does not belong to this notification.")

        rows = connection.execute(
            """SELECT receipt_id, receipt_status, provider_event_at
               FROM notification_delivery_receipts
               WHERE notification_id = ? AND attempt_id = ?
               ORDER BY provider_event_at ASC, receipt_id ASC""",
            (notification_id, attempt_id),
        ).fetchall()
    finally:
        connection.close()

    if not rows:
        return {
            "notification_id": notification_id,
            "attempt_id": attempt_id,
            "status": None,
            "provider_event_at": None,
            "event_count": 0,
            "ignored_event_count": 0,
            "ambiguous_event_count": 0,
            "receipt_ids": (),
        }

    parsed = []
    for receipt_id, status, event_at in rows:
        parsed.append({
            "receipt_id": receipt_id,
            "status": status,
            "event_at": _parse_event_time(event_at),
            "event_at_text": event_at,
        })

    # Group timestamps by their actual instant. This also handles equivalent
    # ISO-8601 offsets that represent the same instant.
    parsed.sort(key=lambda item: (item["event_at"], item["receipt_id"]))
    groups = []
    for event in parsed:
        if not groups or groups[-1][0] != event["event_at"]:
            groups.append((event["event_at"], [event]))
        else:
            groups[-1][1].append(event)

    state = None
    effective_at = None
    ignored_count = 0
    ambiguous_count = 0
    for event_at, events in groups:
        next_state, ignored, ambiguous = _resolve_timestamp_group(state, events)
        if ambiguous:
            ambiguous_count += len(events)
            continue
        if ignored:
            ignored_count += len(events)
            continue
        state = next_state
        effective_at = event_at

    return {
        "notification_id": notification_id,
        "attempt_id": attempt_id,
        "status": state,
        "provider_event_at": effective_at.isoformat() if effective_at else None,
        "event_count": len(parsed),
        "ignored_event_count": ignored_count,
        "ambiguous_event_count": ambiguous_count,
        "receipt_ids": tuple(event["receipt_id"] for event in parsed),
    }
