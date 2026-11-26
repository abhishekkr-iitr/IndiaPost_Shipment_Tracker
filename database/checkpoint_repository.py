import hashlib
import json

from datetime import datetime, timezone

from database.db_connection import get_db_connection


def _normalized_event_date(event_date):
    """
    Return a canonical UTC timestamp for parseable ISO-8601 values.

    Naive timestamps retain the repository's established interpretation
    as UTC. Invalid or missing values are returned unchanged so their
    historical identity behaviour is preserved.
    """

    parsed_date = parse_event_date(event_date)

    if parsed_date is None:
        return event_date

    return parsed_date.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _checkpoint_identity(checkpoint):
    return (
        _normalized_event_date(checkpoint.get("date")),
        checkpoint.get("location"),
        checkpoint.get("status"),
        checkpoint.get("message"),
    )


def generate_checkpoint_fingerprint(checkpoint):
    """
    Generate a unique fingerprint using a canonical timestamp identity.
    """

    checkpoint_identity = {
        "date": _normalized_event_date(checkpoint.get("date")),
        "location": checkpoint.get("location"),
        "status": checkpoint.get("status"),
        "message": checkpoint.get("message")
    }

    checkpoint_string = json.dumps(
        checkpoint_identity,
        sort_keys=True
    )

    return hashlib.sha256(
        checkpoint_string.encode("utf-8")
    ).hexdigest()


def parse_event_date(event_date):
    """
    Convert an ISO 8601 timestamp into a timezone-aware datetime.
    """

    if not event_date:
        return None

    try:
        parsed_date = datetime.fromisoformat(event_date)

        if parsed_date.tzinfo is None:
            parsed_date = parsed_date.replace(
                tzinfo=timezone.utc
            )

        return parsed_date

    except (TypeError, ValueError):
        return None


class CheckpointSaveResult(int):
    """Integer-compatible insert count carrying identities of newly inserted rows."""

    def __new__(cls, inserted_count, inserted_checkpoints):
        instance = int.__new__(cls, inserted_count)
        instance.inserted_checkpoints = tuple(inserted_checkpoints)
        return instance


def save_checkpoints_in_transaction(
    connection,
    shipment_id,
    checkpoints
):
    """
    Save shipment checkpoints using a caller-managed database
    transaction.

    This function does not commit, roll back, or close the
    supplied connection.

    Recalculate the duration between consecutive checkpoints.

    Returns:
        int: Number of newly inserted checkpoints.
    """

    if connection is None:
        raise ValueError("Database connection is required.")

    if not shipment_id:
        raise ValueError("Shipment ID is required.")

    if not isinstance(checkpoints, list):
        raise ValueError("Checkpoints must be provided as a list.")

    cursor = connection.cursor()

    current_time = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )

    inserted_count = 0
    inserted_checkpoints = []

    # Load normalized identities as well as legacy rows. This allows
    # equivalent timestamps to match checkpoints written before the
    # canonical fingerprint format was introduced.
    cursor.execute("""
        SELECT event_date, location, status, message
        FROM checkpoints
        WHERE shipment_id = ?;
    """, (shipment_id,))

    existing_identities = {
        _checkpoint_identity({
            "date": row[0],
            "location": row[1],
            "status": row[2],
            "message": row[3],
        })
        for row in cursor.fetchall()
    }

    # Step 1: Insert new checkpoints

    for checkpoint in checkpoints:

        if not isinstance(checkpoint, dict):
            continue

        identity = _checkpoint_identity(checkpoint)

        # The identity lookup handles legacy fingerprints whose original
        # timestamp strings differ while representing the same instant.
        if identity in existing_identities:
            continue

        fingerprint = generate_checkpoint_fingerprint(
            checkpoint
        )

        cursor.execute("""
            INSERT OR IGNORE INTO checkpoints (

                shipment_id,
                event_date,
                location,
                status,
                message,
                event_fingerprint,
                duration_since_previous_seconds,
                created_at

            )

            VALUES (?, ?, ?, ?, ?, ?, ?, ?);

        """, (

            shipment_id,

            checkpoint.get("date"),

            checkpoint.get("location"),

            checkpoint.get("status"),

            checkpoint.get("message"),

            fingerprint,

            None,

            current_time

        ))

        if cursor.rowcount == 1:
            existing_identities.add(identity)
            inserted_count += 1
            inserted_checkpoints.append({
                "checkpoint_id": cursor.lastrowid,
                "event_date": checkpoint.get("date"),
                "location": checkpoint.get("location"),
                "status": checkpoint.get("status"),
                "message": checkpoint.get("message"),
            })

    # Step 2: Retrieve all checkpoints for this shipment

    cursor.execute("""
        SELECT checkpoint_id, event_date
        FROM checkpoints
        WHERE shipment_id = ?;
    """, (shipment_id,))

    checkpoint_records = cursor.fetchall()

    # Step 3: Sort checkpoints chronologically

    sorted_checkpoints = sorted(
        checkpoint_records,
        key=lambda record: (
            parse_event_date(record[1]) is None,
            parse_event_date(record[1])
            or datetime.max.replace(tzinfo=timezone.utc)
        )
    )

    # Step 4: Calculate duration between consecutive checkpoints

    previous_event_date = None

    for checkpoint_id, event_date in sorted_checkpoints:

        current_event_date = parse_event_date(event_date)

        duration_seconds = None

        if (
            previous_event_date is not None
            and current_event_date is not None
        ):

            duration_seconds = int(
                (
                    current_event_date - previous_event_date
                ).total_seconds()
            )

        cursor.execute("""
            UPDATE checkpoints

            SET duration_since_previous_seconds = ?

            WHERE checkpoint_id = ?;

        """, (
            duration_seconds,
            checkpoint_id
        ))

        if current_event_date is not None:
            previous_event_date = current_event_date

    return CheckpointSaveResult(inserted_count, inserted_checkpoints)


def save_checkpoints(shipment_id, checkpoints):
    """
    Save shipment checkpoints without creating duplicates.

    Recalculate the duration between consecutive checkpoints.

    This public function manages its own database transaction.
    """

    if not shipment_id:
        raise ValueError("Shipment ID is required.")

    if not isinstance(checkpoints, list):
        raise ValueError("Checkpoints must be provided as a list.")

    connection = get_db_connection()

    try:

        inserted_count = save_checkpoints_in_transaction(
            connection,
            shipment_id,
            checkpoints
        )

        connection.commit()

        return inserted_count

    except Exception:

        connection.rollback()

        raise

    finally:

        connection.close()
