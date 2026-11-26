"""Lease-safe outcome transitions for proactive monitoring jobs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from database.db_connection import get_db_connection


MAX_ATTEMPTS = 5
BASE_RETRY_DELAY_MINUTES = 5
MAX_RETRY_DELAY_MINUTES = 6 * 60


def _utc(value=None):
    if value is None:
        value = datetime.now(timezone.utc)
    elif isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise TypeError("now must be a datetime or ISO-8601 string.")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value):
    return value.isoformat(timespec="seconds")


def _owned_job(connection, job_id, worker_id, now_text):
    return connection.execute(
        """
        SELECT attempt_count
        FROM monitoring_jobs
        WHERE monitoring_job_id = ?
          AND status = 'processing'
          AND claimed_by = ?
          AND lease_expires_at > ?
        """,
        (job_id, worker_id, now_text),
    ).fetchone()


def complete_monitoring_cycle(
    job_id: int,
    worker_id: str,
    *,
    shipment_status: str | None,
    now=None,
) -> dict:
    """Atomically advance subscriptions and complete a successful job cycle.

    All active subscribers receive their own next_check_at based on their
    configured interval. The shipment-level job is scheduled for the earliest
    subscriber due time. Delivered shipments complete monitoring for active
    subscriptions and the shared job.
    """
    if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id <= 0:
        raise ValueError("job_id must be a positive integer.")
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise ValueError("worker_id must be a non-empty string.")

    now_dt = _utc(now)
    now_text = _iso(now_dt)
    delivered = str(shipment_status or "").strip().lower() == "delivered"

    connection = get_db_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        job = _owned_job(connection, job_id, worker_id.strip(), now_text)
        if job is None:
            connection.rollback()
            return {"completed": False, "reason": "lease_lost"}

        row = connection.execute(
            "SELECT shipment_id FROM monitoring_jobs WHERE monitoring_job_id = ?",
            (job_id,),
        ).fetchone()
        shipment_id = row[0]

        subscriptions = connection.execute(
            """
            SELECT user_shipment_id, monitoring_interval_hours
            FROM user_shipments
            WHERE shipment_id = ?
              AND monitoring_enabled = 1
              AND monitoring_completed_at IS NULL
            """,
            (shipment_id,),
        ).fetchall()

        if delivered:
            connection.execute(
                """
                UPDATE user_shipments
                SET next_check_at = NULL,
                    monitoring_completed_at = ?
                WHERE shipment_id = ?
                  AND monitoring_enabled = 1
                  AND monitoring_completed_at IS NULL
                """,
                (now_text, shipment_id),
            )
            connection.execute(
                """
                UPDATE monitoring_jobs
                SET status = 'completed',
                    next_run_at = NULL,
                    next_retry_at = NULL,
                    claimed_by = NULL,
                    claimed_at = NULL,
                    lease_expires_at = NULL,
                    last_success_at = ?,
                    last_error = NULL,
                    attempt_count = 0,
                    updated_at = ?
                WHERE monitoring_job_id = ?
                  AND status = 'processing'
                  AND claimed_by = ?
                  AND lease_expires_at > ?
                """,
                (now_text, now_text, job_id, worker_id.strip(), now_text),
            )
            completed = connection.execute(
                "SELECT status FROM monitoring_jobs WHERE monitoring_job_id = ?",
                (job_id,),
            ).fetchone()[0] == "completed"
            connection.commit()
            return {
                "completed": completed,
                "status": "completed" if completed else None,
                "delivered": True,
            }

        if not subscriptions:
            connection.execute(
                """
                UPDATE monitoring_jobs
                SET status = 'disabled',
                    next_run_at = NULL,
                    next_retry_at = NULL,
                    claimed_by = NULL,
                    claimed_at = NULL,
                    lease_expires_at = NULL,
                    last_success_at = ?,
                    last_error = NULL,
                    attempt_count = 0,
                    updated_at = ?
                WHERE monitoring_job_id = ?
                  AND status = 'processing'
                  AND claimed_by = ?
                  AND lease_expires_at > ?
                """,
                (now_text, now_text, job_id, worker_id.strip(), now_text),
            )
            status = connection.execute(
                "SELECT status FROM monitoring_jobs WHERE monitoring_job_id = ?",
                (job_id,),
            ).fetchone()[0]
            connection.commit()
            return {"completed": status == "disabled", "status": status}

        due_times = []
        for subscription_id, interval_hours in subscriptions:
            if (
                not isinstance(interval_hours, int)
                or isinstance(interval_hours, bool)
                or interval_hours <= 0
            ):
                raise ValueError(
                    f"Invalid monitoring interval for subscription {subscription_id}."
                )
            due = now_dt + timedelta(hours=interval_hours)
            due_text = _iso(due)
            connection.execute(
                """
                UPDATE user_shipments
                SET next_check_at = ?
                WHERE user_shipment_id = ?
                  AND monitoring_enabled = 1
                  AND monitoring_completed_at IS NULL
                """,
                (due_text, subscription_id),
            )
            due_times.append(due)

        next_run_text = _iso(min(due_times))
        cursor = connection.execute(
            """
            UPDATE monitoring_jobs
            SET status = 'scheduled',
                next_run_at = ?,
                next_retry_at = NULL,
                claimed_by = NULL,
                claimed_at = NULL,
                lease_expires_at = NULL,
                last_success_at = ?,
                last_error = NULL,
                attempt_count = 0,
                updated_at = ?
            WHERE monitoring_job_id = ?
              AND status = 'processing'
              AND claimed_by = ?
              AND lease_expires_at > ?
            """,
            (
                next_run_text, now_text, now_text, job_id,
                worker_id.strip(), now_text,
            ),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return {"completed": False, "reason": "lease_lost"}
        connection.commit()
        return {
            "completed": True,
            "status": "scheduled",
            "next_run_at": next_run_text,
            "subscriber_count": len(subscriptions),
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def record_monitoring_failure(
    job_id: int,
    worker_id: str,
    *,
    error: str,
    now=None,
) -> dict:
    """Schedule bounded exponential retry, or leave exhausted job dormant.

    attempt_count includes the initial attempt. At the fifth failed attempt,
    the job remains retryable with next_retry_at NULL, preserving its error
    while excluding it from due-job selection.
    """
    if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id <= 0:
        raise ValueError("job_id must be a positive integer.")
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise ValueError("worker_id must be a non-empty string.")
    if not isinstance(error, str) or not error.strip():
        error = "Monitoring tracking attempt failed."

    now_dt = _utc(now)
    now_text = _iso(now_dt)
    connection = get_db_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        job = _owned_job(connection, job_id, worker_id.strip(), now_text)
        if job is None:
            connection.rollback()
            return {"recorded": False, "reason": "lease_lost"}

        attempts = int(job[0])
        exhausted = attempts >= MAX_ATTEMPTS
        if exhausted:
            retry_text = None
            result = {
                "recorded": True,
                "exhausted": True,
                "attempt_count": attempts,
                "next_retry_at": None,
            }
        else:
            delay = min(
                BASE_RETRY_DELAY_MINUTES * (2 ** max(attempts - 1, 0)),
                MAX_RETRY_DELAY_MINUTES,
            )
            retry_text = _iso(now_dt + timedelta(minutes=delay))
            result = {
                "recorded": True,
                "exhausted": False,
                "attempt_count": attempts,
                "retry_delay_minutes": delay,
                "next_retry_at": retry_text,
            }

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
            (
                retry_text, error.strip()[:2000], now_text,
                job_id, worker_id.strip(), now_text,
            ),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return {"recorded": False, "reason": "lease_lost"}
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
