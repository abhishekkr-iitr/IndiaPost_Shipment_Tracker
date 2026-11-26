"""Repository for durable AfterShip webhook event persistence and processing."""

import json
import uuid

from datetime import datetime, timedelta, timezone

from database.db_connection import get_db_connection


DEFAULT_MAX_PROCESSING_ATTEMPTS = 5

DEFAULT_RETRY_DELAYS_SECONDS = (
    60,
    300,
    900,
    3600,
    21600,
)


def _utc_now():
    """Return the current timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def _utc_iso(value=None):
    """Return a consistently formatted UTC ISO timestamp."""
    if value is None:
        value = _utc_now()

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    )


def _row_to_dict(cursor, row):
    """Convert a SQLite row tuple into a dictionary."""
    if row is None:
        return None

    columns = [
        description[0]
        for description in cursor.description
    ]

    return dict(zip(columns, row))


def save_webhook_event(payload, raw_payload, provider="aftership"):
    """
    Persist a validated webhook event.

    Duplicate provider event IDs return the existing record.

    Returns:
        {
            "event_record_id": str,
            "inserted": bool
        }
    """

    if not isinstance(payload, dict):
        raise ValueError("Webhook payload must be a dictionary.")

    if not isinstance(raw_payload, bytes):
        raise ValueError("Raw webhook payload must be bytes.")

    if not raw_payload:
        raise ValueError("Raw webhook payload cannot be empty.")

    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("Provider is required.")

    event_id = payload.get("event_id")
    event_type = payload.get("event")
    event_timestamp = payload.get("ts")
    message = payload.get("msg")

    if not isinstance(event_id, str) or not event_id:
        raise ValueError("Provider event ID is required.")

    if not isinstance(event_type, str) or not event_type:
        raise ValueError("Event type is required.")

    if (
        isinstance(event_timestamp, bool)
        or not isinstance(event_timestamp, (int, float))
    ):
        raise ValueError("Provider event timestamp must be numeric.")

    if not isinstance(message, dict):
        raise ValueError("Webhook message must be a dictionary.")

    try:
        raw_payload_text = raw_payload.decode("utf-8")
        json.loads(raw_payload_text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError(
            "Raw webhook payload must contain valid UTF-8 JSON."
        ) from None

    event_record_id = str(uuid.uuid4())

    received_at = _utc_iso()

    provider_tracking_id = message.get("id")
    tracking_number = message.get("tracking_number")
    carrier = message.get("slug")

    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            INSERT INTO webhook_events (
                event_record_id,
                provider,
                provider_event_id,
                event_type,
                provider_event_timestamp,
                provider_tracking_id,
                tracking_number,
                carrier,
                raw_payload,
                received_at,
                status,
                processing_attempts
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'received', 0)
            ON CONFLICT(provider, provider_event_id) DO NOTHING
            """,
            (
                event_record_id,
                provider.strip().lower(),
                event_id,
                event_type,
                event_timestamp,
                provider_tracking_id,
                tracking_number,
                carrier,
                raw_payload_text,
                received_at,
            ),
        )

        inserted = cursor.rowcount == 1

        if inserted:
            saved_record_id = event_record_id
        else:
            cursor.execute(
                """
                SELECT event_record_id
                FROM webhook_events
                WHERE provider = ?
                  AND provider_event_id = ?
                """,
                (provider.strip().lower(), event_id),
            )

            existing = cursor.fetchone()

            if existing is None:
                raise RuntimeError(
                    "Webhook event insertion failed unexpectedly."
                )

            saved_record_id = existing[0]

        connection.commit()

        return {
            "event_record_id": saved_record_id,
            "inserted": inserted,
        }

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


def get_webhook_event(event_record_id):
    """Retrieve a webhook event by its internal record ID."""

    if not event_record_id:
        raise ValueError("Event record ID is required.")

    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            SELECT *
            FROM webhook_events
            WHERE event_record_id = ?
            """,
            (event_record_id,),
        )

        return _row_to_dict(cursor, cursor.fetchone())

    finally:
        connection.close()


def get_webhook_event_by_provider_id(
    provider_event_id,
    provider="aftership",
):
    """Retrieve an event using its provider and provider event ID."""

    if not provider_event_id:
        raise ValueError("Provider event ID is required.")

    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("Provider is required.")

    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            SELECT *
            FROM webhook_events
            WHERE provider = ?
              AND provider_event_id = ?
            """,
            (provider.strip().lower(), provider_event_id),
        )

        return _row_to_dict(cursor, cursor.fetchone())

    finally:
        connection.close()

def list_webhook_events(
    status=None,
    provider=None,
    tracking_number=None,
    limit=100,
    offset=0,
):
    """
    List webhook events with optional filters.

    Results are ordered newest first.
    The default page size is 100; the maximum is 500.
    """

    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 500
    ):
        raise ValueError("Limit must be an integer between 1 and 500.")

    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
    ):
        raise ValueError("Offset must be a non-negative integer.")

    filters = []
    parameters = []

    if status is not None:
        if not isinstance(status, str) or not status.strip():
            raise ValueError("Status must be a non-empty string.")

        filters.append("status = ?")
        parameters.append(status.strip().lower())

    if provider is not None:
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("Provider must be a non-empty string.")

        filters.append("provider = ?")
        parameters.append(provider.strip().lower())

    if tracking_number is not None:
        if (
            not isinstance(tracking_number, str)
            or not tracking_number.strip()
        ):
            raise ValueError(
                "Tracking number must be a non-empty string."
            )

        filters.append("tracking_number = ?")
        parameters.append(tracking_number.strip())

    query = """
        SELECT *
        FROM webhook_events
    """

    if filters:
        query += " WHERE " + " AND ".join(filters)

    query += """
        ORDER BY received_at DESC, event_record_id DESC
        LIMIT ? OFFSET ?
    """

    parameters.extend([limit, offset])

    connection = get_db_connection()

    try:
        cursor = connection.cursor()
        cursor.execute(query, parameters)

        rows = cursor.fetchall()

        return [
            _row_to_dict(cursor, row)
            for row in rows
        ]

    finally:
        connection.close()

def claim_next_webhook_event(
    worker_id,
    lease_seconds=300,
    max_attempts=DEFAULT_MAX_PROCESSING_ATTEMPTS,
):
    """
    Atomically claim the next eligible webhook event.

    Eligible events:
        - received
        - retryable with a due retry time
        - processing with an expired lease

    Uses BEGIN IMMEDIATE to serialize competing SQLite writers.

    The processing attempt counter increments when an event is
    claimed, including when reclaiming an expired lease.

    Returns:
        Event dictionary if an event was claimed.
        None if no eligible event exists.
    """

    if not isinstance(worker_id, str) or not worker_id.strip():
        raise ValueError("Worker ID is required.")

    if (
        isinstance(lease_seconds, bool)
        or not isinstance(lease_seconds, int)
        or lease_seconds <= 0
    ):
        raise ValueError("Lease duration must be a positive integer.")

    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or max_attempts <= 0
    ):
        raise ValueError("Maximum attempts must be a positive integer.")

    now = _utc_now()
    now_text = _utc_iso(now)

    lease_expires_at = _utc_iso(
        now + timedelta(seconds=lease_seconds)
    )

    connection = get_db_connection()

    try:
        connection.execute("BEGIN IMMEDIATE")

        cursor = connection.cursor()

        # Recover events whose final processing attempt crashed
        # and whose worker lease has expired.
        cursor.execute(
            """
            UPDATE webhook_events

            SET
                status = 'dead_letter',
                next_retry_at = NULL,
                claimed_by = NULL,
                claimed_at = NULL,
                lease_expires_at = NULL,
                last_error = CASE
                    WHEN last_error IS NULL OR last_error = ''
                    THEN
                        'Processing lease expired after maximum processing attempts.'
                    ELSE
                        substr(
                            last_error || ' | Processing lease expired after maximum processing attempts.',
                            1,
                            2000
                        )
                END,
                processed_at = NULL

            WHERE status = 'processing'
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at <= ?
              AND processing_attempts >= ?
            """,
            (
                now_text,
                max_attempts,
            ),
        )

        cursor.execute(
            """
            SELECT event_record_id
            FROM webhook_events
            WHERE
                (
                    status = 'received'

                    OR
                    (
                        status = 'retryable'
                        AND (
                            next_retry_at IS NULL
                            OR next_retry_at <= ?
                        )
                    )

                    OR
                    (
                        status = 'processing'
                        AND lease_expires_at IS NOT NULL
                        AND lease_expires_at <= ?
                    )
                )
                AND processing_attempts < ?
            ORDER BY received_at ASC
            LIMIT 1
            """,
            (
                now_text,
                now_text,
                max_attempts,
            ),
        )

        selected = cursor.fetchone()

        if selected is None:
            connection.commit()
            return None

        event_record_id = selected[0]

        cursor.execute(
            """
            UPDATE webhook_events

            SET
                status = 'processing',
                claimed_by = ?,
                claimed_at = ?,
                lease_expires_at = ?,
                processing_attempts = processing_attempts + 1,
                next_retry_at = NULL,
                last_error = NULL

            WHERE event_record_id = ?

              AND processing_attempts < ?

              AND (
                    status = 'received'

                    OR (
                        status = 'retryable'
                        AND (
                            next_retry_at IS NULL
                            OR next_retry_at <= ?
                        )
                    )

                    OR (
                        status = 'processing'
                        AND lease_expires_at IS NOT NULL
                        AND lease_expires_at <= ?
                    )
              )
            """,
            (
                worker_id.strip(),
                now_text,
                lease_expires_at,
                event_record_id,
                max_attempts,
                now_text,
                now_text,
            ),
        )

        if cursor.rowcount != 1:
            connection.commit()
            return None

        cursor.execute(
            """
            SELECT *
            FROM webhook_events
            WHERE event_record_id = ?
            """,
            (event_record_id,),
        )

        claimed_event = _row_to_dict(
            cursor,
            cursor.fetchone()
        )

        connection.commit()

        return claimed_event

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


def mark_webhook_event_processed_in_transaction(
    connection,
    event_record_id,
    worker_id,
):
    """
    Mark a claimed event as processed using a caller-managed
    database transaction.

    Does not commit, roll back, or close the supplied connection.

    Intended to be executed in the same transaction as shipment
    and checkpoint updates.

    Raises RuntimeError if the worker does not own the claim.
    """

    if connection is None:
        raise ValueError("Database connection is required.")

    if not event_record_id:
        raise ValueError("Event record ID is required.")

    if not isinstance(worker_id, str) or not worker_id.strip():
        raise ValueError("Worker ID is required.")

    cursor = connection.cursor()

    cursor.execute(
        """
        UPDATE webhook_events

        SET
            status = 'processed',
            processed_at = ?,
            next_retry_at = NULL,
            claimed_by = NULL,
            claimed_at = NULL,
            lease_expires_at = NULL,
            last_error = NULL

        WHERE event_record_id = ?
          AND status = 'processing'
          AND claimed_by = ?
        """,
        (
            _utc_iso(),
            event_record_id,
            worker_id.strip(),
        ),
    )

    if cursor.rowcount != 1:
        raise RuntimeError(
            "Webhook event cannot be marked processed: "
            "event is not currently claimed by this worker."
        )

    return True


def mark_webhook_event_failed(
    event_record_id,
    worker_id,
    error,
    retry_delays=DEFAULT_RETRY_DELAYS_SECONDS,
    max_attempts=DEFAULT_MAX_PROCESSING_ATTEMPTS,
):
    """
    Record a processing failure and schedule a retry or dead-letter.

    The attempt count is incremented during claiming, not here.

    Events reaching max_attempts are moved to dead_letter.

    Returns:
        {
            "status": "retryable" or "dead_letter",
            "next_retry_at": timestamp or None,
            "processing_attempts": int
        }
    """

    if not event_record_id:
        raise ValueError("Event record ID is required.")

    if not isinstance(worker_id, str) or not worker_id.strip():
        raise ValueError("Worker ID is required.")

    if not isinstance(error, str) or not error.strip():
        raise ValueError("A non-empty processing error is required.")

    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or max_attempts <= 0
    ):
        raise ValueError("Maximum attempts must be a positive integer.")

    if not isinstance(retry_delays, (tuple, list)):
        raise ValueError("Retry delays must be a list or tuple.")

    if not retry_delays:
        raise ValueError("At least one retry delay is required.")

    for delay in retry_delays:
        if (
            isinstance(delay, bool)
            or not isinstance(delay, int)
            or delay < 0
        ):
            raise ValueError(
                "Retry delays must be non-negative integers."
            )

    connection = get_db_connection()

    try:
        connection.execute("BEGIN IMMEDIATE")

        cursor = connection.cursor()

        cursor.execute(
            """
            SELECT processing_attempts
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

        result = cursor.fetchone()

        if result is None:
            raise RuntimeError(
                "Cannot record failure: event is not claimed "
                "by this worker."
            )

        attempts = result[0]

        if attempts >= max_attempts:
            new_status = "dead_letter"
            next_retry_at = None

        else:
            new_status = "retryable"

            delay_index = min(
                attempts - 1,
                len(retry_delays) - 1
            )

            retry_delay = retry_delays[delay_index]

            next_retry_at = _utc_iso(
                _utc_now() + timedelta(seconds=retry_delay)
            )

        cursor.execute(
            """
            UPDATE webhook_events

            SET
                status = ?,
                next_retry_at = ?,
                claimed_by = NULL,
                claimed_at = NULL,
                lease_expires_at = NULL,
                last_error = ?,
                processed_at = NULL

            WHERE event_record_id = ?
              AND status = 'processing'
              AND claimed_by = ?
            """,
            (
                new_status,
                next_retry_at,
                error.strip()[:2000],
                event_record_id,
                worker_id.strip(),
            ),
        )

        if cursor.rowcount != 1:
            raise RuntimeError(
                "Webhook event failure update was not applied."
            )

        connection.commit()

        return {
            "status": new_status,
            "next_retry_at": next_retry_at,
            "processing_attempts": attempts,
        }

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()