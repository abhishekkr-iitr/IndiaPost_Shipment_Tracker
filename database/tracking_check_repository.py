from datetime import datetime

from database.db_connection import get_db_connection


def start_tracking_check(shipment_id):
    """
    Create a new tracking-check record when a tracking
    attempt begins.
    """

    if not shipment_id:
        raise ValueError("Shipment ID is required.")

    connection = get_db_connection()

    try:

        cursor = connection.cursor()

        current_time = datetime.now().astimezone().isoformat(
            timespec="seconds"
        )

        cursor.execute("""
            INSERT INTO tracking_checks (

                shipment_id,
                check_started_at,
                check_completed_at,
                check_status,
                retrieved_status,
                error_message

            )

            VALUES (?, ?, ?, ?, ?, ?);

        """, (

            shipment_id,
            current_time,
            None,
            "in_progress",
            None,
            None

        ))

        connection.commit()

        check_id = cursor.lastrowid

        return check_id

    except Exception:

        connection.rollback()

        raise

    finally:

        connection.close()


def complete_tracking_check(
    check_id,
    check_status,
    retrieved_status=None,
    error_message=None
):
    """
    Update a tracking-check record when the tracking
    attempt finishes.
    """

    if not check_id:
        raise ValueError("Tracking Check ID is required.")

    if check_status not in ("success", "failed"):
        raise ValueError(
            "Check status must be either 'success' or 'failed'."
        )

    connection = get_db_connection()

    try:

        cursor = connection.cursor()

        current_time = datetime.now().astimezone().isoformat(
            timespec="seconds"
        )

        cursor.execute("""
            UPDATE tracking_checks

            SET
                check_completed_at = ?,
                check_status = ?,
                retrieved_status = ?,
                error_message = ?

            WHERE
                check_id = ?
                AND check_status = 'in_progress';

        """, (

            current_time,
            check_status,
            retrieved_status,
            error_message,
            check_id

        ))

        if cursor.rowcount == 0:

            raise ValueError(
                "Tracking check not found or already completed."
            )

        connection.commit()

        return True

    except Exception:

        connection.rollback()

        raise

    finally:

        connection.close()