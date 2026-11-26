"""SQLite repository for shipment-level proactive monitoring jobs.

This module manages scheduling and execution ownership only. It does not call
tracking providers or send notifications.

All persisted timestamps are normalised to UTC ISO-8601 strings. Repository
operations use short transactions; claim operations acquire a SQLite write
reservation before selecting candidates so concurrent workers cannot claim
the same job.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from database.db_connection import get_db_connection


ACTIVE_SUBSCRIPTION_PREDICATE = """
    monitoring_enabled = 1
    AND monitoring_completed_at IS NULL
"""


class MonitoringJobError(RuntimeError):
    """Raised when a monitoring job cannot safely transition state."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_utc(value: str | datetime | None, *, null_value: datetime | None = None) -> datetime:
    if value is None:
        if null_value is None:
            raise ValueError("A timestamp is required.")
        value = null_value

    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"Invalid ISO-8601 timestamp: {value!r}") from exc

    if not isinstance(value, datetime):
        raise TypeError("Timestamp must be an ISO-8601 string or datetime.")

    if value.tzinfo is None:
        # Match Migration 6's deterministic legacy timestamp policy.
        value = value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc)


def _iso_utc(value: str | datetime | None, *, null_value: datetime | None = None) -> str:
    return _to_utc(value, null_value=null_value).isoformat(timespec="seconds")


def _row_to_dict(cursor, row) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(zip((item[0] for item in cursor.description), row))


def _get_subscription_schedule(connection, shipment_id: int, now: datetime):
    rows = list(connection.execute(
        f"""
        SELECT next_check_at
        FROM user_shipments
        WHERE shipment_id = ?
          AND {ACTIVE_SUBSCRIPTION_PREDICATE}
        """,
        (shipment_id,),
    ).fetchall())

    has_guest_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='guest_shipment_subscriptions'"
    ).fetchone() is not None

    if has_guest_table:
        guest_rows = connection.execute(
            f"""
            SELECT next_check_at
            FROM guest_shipment_subscriptions
            WHERE shipment_id = ?
              AND {ACTIVE_SUBSCRIPTION_PREDICATE}
            """,
            (shipment_id,),
        ).fetchall()
        rows.extend(guest_rows)

    if not rows:
        return None

    due_times = [
        _to_utc(row[0], null_value=now)
        for row in rows
    ]
    return min(due_times)


def _sync_monitoring_job_in_transaction(
    connection, shipment_id: int, *, now: str | datetime | None = None
) -> dict:
    """Synchronise a shipment job using the caller's active transaction."""
    if not isinstance(shipment_id, int) or isinstance(shipment_id, bool) or shipment_id <= 0:
        raise ValueError("shipment_id must be a positive integer.")

    now_dt = _to_utc(now, null_value=_utc_now())
    now_text = _iso_utc(now_dt)

    shipment = connection.execute(
        "SELECT current_status FROM shipments WHERE shipment_id = ?",
        (shipment_id,),
    ).fetchone()
    if shipment is None:
        raise MonitoringJobError(f"Shipment {shipment_id} does not exist.")

    existing = connection.execute(
        "SELECT monitoring_job_id, status FROM monitoring_jobs WHERE shipment_id = ?",
        (shipment_id,),
    ).fetchone()

    if str(shipment[0] or "").strip().lower() == "delivered":
        if existing:
            connection.execute(
                """
                UPDATE monitoring_jobs
                SET status = 'completed', next_run_at = NULL,
                    next_retry_at = NULL, claimed_by = NULL,
                    claimed_at = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE shipment_id = ?
                """,
                (now_text, shipment_id),
            )
        else:
            connection.execute(
                """
                INSERT INTO monitoring_jobs
                    (shipment_id, status, next_run_at, attempt_count,
                     created_at, updated_at)
                VALUES (?, 'completed', NULL, 0, ?, ?)
                """,
                (shipment_id, now_text, now_text),
            )
    else:
        next_due = _get_subscription_schedule(connection, shipment_id, now_dt)

        if next_due is None:
            if existing:
                connection.execute(
                    """
                    UPDATE monitoring_jobs
                    SET status = 'disabled', next_run_at = NULL,
                        next_retry_at = NULL, claimed_by = NULL,
                        claimed_at = NULL, lease_expires_at = NULL,
                        updated_at = ?
                    WHERE shipment_id = ? AND status <> 'processing'
                    """,
                    (now_text, shipment_id),
                )
            # A currently processing job is left untouched.
        elif existing is None:
            connection.execute(
                """
                INSERT INTO monitoring_jobs
                    (shipment_id, status, next_run_at, attempt_count,
                     created_at, updated_at)
                VALUES (?, 'scheduled', ?, 0, ?, ?)
                """,
                (shipment_id, _iso_utc(next_due), now_text, now_text),
            )
        elif existing[1] != "processing":
            connection.execute(
                """
                UPDATE monitoring_jobs
                SET status = 'scheduled', next_run_at = ?,
                    next_retry_at = NULL, claimed_by = NULL,
                    claimed_at = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE shipment_id = ?
                """,
                (_iso_utc(next_due), now_text, shipment_id),
            )

    cursor = connection.execute(
        "SELECT * FROM monitoring_jobs WHERE shipment_id = ?",
        (shipment_id,),
    )
    return _row_to_dict(cursor, cursor.fetchone())


def sync_monitoring_job(shipment_id: int, *, now: str | datetime | None = None) -> dict:
    """Synchronise one shipment's job with its current subscriptions."""
    if not isinstance(shipment_id, int) or isinstance(shipment_id, bool) or shipment_id <= 0:
        raise ValueError("shipment_id must be a positive integer.")
    connection = get_db_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        result = _sync_monitoring_job_in_transaction(connection, shipment_id, now=now)
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_monitoring_job(shipment_id: int) -> dict | None:
    connection = get_db_connection()
    try:
        cursor = connection.execute(
            "SELECT * FROM monitoring_jobs WHERE shipment_id = ?",
            (shipment_id,),
        )
        return _row_to_dict(cursor, cursor.fetchone())
    finally:
        connection.close()


def list_due_monitoring_jobs(
    *, limit: int = 50, now: str | datetime | None = None
) -> list[dict]:
    """Return due scheduled/retryable jobs without claiming them.

    This is an informational query only. Workers should use
    claim_due_monitoring_jobs() to obtain exclusive execution ownership.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("limit must be a positive integer.")

    now_text = _iso_utc(now, null_value=_utc_now())
    connection = get_db_connection()
    try:
        cursor = connection.execute(
            """
            SELECT *
            FROM monitoring_jobs
            WHERE (
                status = 'scheduled' AND next_run_at IS NOT NULL
                AND next_run_at <= ?
            ) OR (
                status = 'retryable' AND next_retry_at IS NOT NULL
                AND next_retry_at <= ?
            )
            ORDER BY
                CASE WHEN status = 'retryable' THEN next_retry_at
                     ELSE next_run_at END,
                monitoring_job_id
            LIMIT ?
            """,
            (now_text, now_text, limit),
        )
        return [_row_to_dict(cursor, row) for row in cursor.fetchall()]
    finally:
        connection.close()


def claim_due_monitoring_jobs(
    worker_id: str,
    *,
    limit: int = 1,
    lease_seconds: int = 300,
    now: str | datetime | None = None,
) -> list[dict]:
    """Atomically claim due jobs for one worker.

    BEGIN IMMEDIATE serialises competing SQLite writers. The status predicate
    is repeated in each UPDATE as a defensive compare-and-set.
    """
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise ValueError("worker_id must be a non-empty string.")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("limit must be a positive integer.")
    if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds <= 0:
        raise ValueError("lease_seconds must be a positive integer.")

    now_dt = _to_utc(now, null_value=_utc_now())
    now_text = _iso_utc(now_dt)
    lease_until = _iso_utc(now_dt + timedelta(seconds=lease_seconds))
    connection = get_db_connection()

    try:
        connection.execute("BEGIN IMMEDIATE")
        candidates = connection.execute(
            """
            SELECT monitoring_job_id
            FROM monitoring_jobs
            WHERE (
                status = 'scheduled' AND next_run_at IS NOT NULL
                AND next_run_at <= ?
            ) OR (
                status = 'retryable' AND next_retry_at IS NOT NULL
                AND next_retry_at <= ?
            )
            ORDER BY
                CASE WHEN status = 'retryable' THEN next_retry_at
                     ELSE next_run_at END,
                monitoring_job_id
            LIMIT ?
            """,
            (now_text, now_text, limit),
        ).fetchall()

        claimed_ids = []
        for (job_id,) in candidates:
            cursor = connection.execute(
                """
                UPDATE monitoring_jobs
                SET status = 'processing',
                    claimed_by = ?,
                    claimed_at = ?,
                    lease_expires_at = ?,
                    attempt_count = attempt_count + 1,
                    last_attempt_at = ?,
                    next_retry_at = NULL,
                    updated_at = ?
                WHERE monitoring_job_id = ?
                  AND status IN ('scheduled', 'retryable')
                """,
                (worker_id, now_text, lease_until, now_text, now_text, job_id),
            )
            if cursor.rowcount == 1:
                claimed_ids.append(job_id)

        claimed = []
        for job_id in claimed_ids:
            cursor = connection.execute(
                "SELECT * FROM monitoring_jobs WHERE monitoring_job_id = ?",
                (job_id,),
            )
            claimed.append(_row_to_dict(cursor, cursor.fetchone()))

        connection.commit()
        return claimed
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def renew_monitoring_job_lease(
    job_id: int,
    worker_id: str,
    *,
    lease_seconds: int = 300,
    now: str | datetime | None = None,
) -> bool:
    """Renew a lease only while this worker still owns an unexpired lease."""
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive.")
    now_dt = _to_utc(now, null_value=_utc_now())
    now_text = _iso_utc(now_dt)
    lease_until = _iso_utc(now_dt + timedelta(seconds=lease_seconds))
    connection = get_db_connection()
    try:
        cursor = connection.execute(
            """
            UPDATE monitoring_jobs
            SET lease_expires_at = ?, updated_at = ?
            WHERE monitoring_job_id = ?
              AND status = 'processing'
              AND claimed_by = ?
              AND lease_expires_at > ?
            """,
            (lease_until, now_text, job_id, worker_id, now_text),
        )
        connection.commit()
        return cursor.rowcount == 1
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def recover_expired_monitoring_job_leases(
    *, now: str | datetime | None = None, limit: int = 100
) -> int:
    """Move expired processing leases to retryable for later retry handling."""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("limit must be a positive integer.")
    now_text = _iso_utc(now, null_value=_utc_now())
    connection = get_db_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            UPDATE monitoring_jobs
            SET status = 'retryable',
                claimed_by = NULL,
                claimed_at = NULL,
                lease_expires_at = NULL,
                next_retry_at = ?,
                last_error = COALESCE(last_error, 'Execution lease expired.'),
                updated_at = ?
            WHERE monitoring_job_id IN (
                SELECT monitoring_job_id
                FROM monitoring_jobs
                WHERE status = 'processing'
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at <= ?
                ORDER BY lease_expires_at, monitoring_job_id
                LIMIT ?
            )
            """,
            (now_text, now_text, now_text, limit),
        )
        count = cursor.rowcount
        connection.commit()
        return count
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def complete_monitoring_job(
    job_id: int,
    worker_id: str,
    *,
    next_run_at: str | datetime | None,
    delivered: bool = False,
    now: str | datetime | None = None,
) -> bool:
    """Complete a claimed job and schedule its next run, or stop if delivered.

    The caller supplies the next run calculated from active subscription
    intervals. A stale worker cannot complete a job after its lease expires.
    """
    now_text = _iso_utc(now, null_value=_utc_now())
    next_status = "completed" if delivered else "scheduled"
    next_run_text = None if delivered else _iso_utc(next_run_at)
    connection = get_db_connection()
    try:
        cursor = connection.execute(
            """
            UPDATE monitoring_jobs
            SET status = ?,
                next_run_at = ?,
                next_retry_at = NULL,
                claimed_by = NULL,
                claimed_at = NULL,
                lease_expires_at = NULL,
                last_success_at = ?,
                last_error = NULL,
                updated_at = ?
            WHERE monitoring_job_id = ?
              AND status = 'processing'
              AND claimed_by = ?
              AND lease_expires_at > ?
            """,
            (
                next_status, next_run_text, now_text, now_text,
                job_id, worker_id, now_text,
            ),
        )
        connection.commit()
        return cursor.rowcount == 1
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def schedule_monitoring_job_retry(
    job_id: int,
    worker_id: str,
    *,
    retry_at: str | datetime,
    error: str,
    now: str | datetime | None = None,
) -> bool:
    """Schedule a retry if the caller still owns an unexpired lease."""
    now_text = _iso_utc(now, null_value=_utc_now())
    retry_text = _iso_utc(retry_at)
    connection = get_db_connection()
    try:
        cursor = connection.execute(
            """
            UPDATE monitoring_jobs
            SET status = 'retryable',
                next_retry_at = ?,
                claimed_by = NULL,
                claimed_at = NULL,
                lease_expires_at = NULL,
                last_error = ?,
                updated_at = ?
            WHERE monitoring_job_id = ?
              AND status = 'processing'
              AND claimed_by = ?
              AND lease_expires_at > ?
            """,
            (retry_text, str(error)[:2000], now_text, job_id, worker_id, now_text),
        )
        connection.commit()
        return cursor.rowcount == 1
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def disable_monitoring_job(
    shipment_id: int, *, now: str | datetime | None = None
) -> bool:
    """Disable a non-processing job for a shipment."""
    now_text = _iso_utc(now, null_value=_utc_now())
    connection = get_db_connection()
    try:
        cursor = connection.execute(
            """
            UPDATE monitoring_jobs
            SET status = 'disabled', next_run_at = NULL, next_retry_at = NULL,
                claimed_by = NULL, claimed_at = NULL, lease_expires_at = NULL,
                updated_at = ?
            WHERE shipment_id = ? AND status <> 'processing'
            """,
            (now_text, shipment_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def complete_monitoring_for_delivered_shipment(
    shipment_id: int, *, now: str | datetime | None = None
) -> bool:
    """Stop routine monitoring after the shipment is confirmed Delivered."""
    now_text = _iso_utc(now, null_value=_utc_now())
    connection = get_db_connection()
    try:
        cursor = connection.execute(
            """
            UPDATE monitoring_jobs
            SET status = 'completed', next_run_at = NULL, next_retry_at = NULL,
                claimed_by = NULL, claimed_at = NULL, lease_expires_at = NULL,
                updated_at = ?
            WHERE shipment_id = ?
            """,
            (now_text, shipment_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
