from datetime import datetime, timedelta

from database.db_connection import get_db_connection


def save_user_shipment(
    user_id,
    shipment_id,
    monitoring_interval_hours=6,
    monitoring_enabled=1
):
    """
    Create or update a user's shipment subscription.

    Each user can independently configure shipment monitoring.
    """

    # Step 1: Validate inputs

    if not user_id:
        raise ValueError("User ID is required.")

    if not shipment_id:
        raise ValueError("Shipment ID is required.")

    if (
        not isinstance(monitoring_interval_hours, int)
        or isinstance(monitoring_interval_hours, bool)
        or monitoring_interval_hours <= 0
    ):
        raise ValueError(
            "Monitoring interval must be a positive integer."
        )

    if monitoring_enabled not in (0, 1):
        raise ValueError(
            "Monitoring enabled must be either 0 or 1."
        )

    # Step 2: Calculate monitoring timestamps

    current_time = datetime.now().astimezone()

    current_timestamp = current_time.isoformat(
        timespec="seconds"
    )

    if monitoring_enabled == 1:

        next_check = current_time + timedelta(
            hours=monitoring_interval_hours
        )

        next_check_at = next_check.isoformat(
            timespec="seconds"
        )

        monitoring_completed_at = None

    else:

        next_check_at = None

        monitoring_completed_at = None

    # Step 3: Connect to the database

    connection = get_db_connection()

    try:

        cursor = connection.cursor()

        # Step 4: Insert or update subscription

        cursor.execute("""
            INSERT INTO user_shipments (

                user_id,
                shipment_id,
                monitoring_enabled,
                monitoring_interval_hours,
                next_check_at,
                monitoring_completed_at,
                created_at

            )

            VALUES (?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(user_id, shipment_id)

            DO UPDATE SET

                monitoring_enabled =
                    excluded.monitoring_enabled,

                monitoring_interval_hours =
                    excluded.monitoring_interval_hours,

                next_check_at =
                    excluded.next_check_at,

                monitoring_completed_at =
                    excluded.monitoring_completed_at;

        """, (

            user_id,
            shipment_id,
            monitoring_enabled,
            monitoring_interval_hours,
            next_check_at,
            monitoring_completed_at,
            current_timestamp

        ))

        # Step 5: Retrieve subscription ID within the same transaction.

        cursor.execute("""
            SELECT user_shipment_id

            FROM user_shipments

            WHERE user_id = ?
            AND shipment_id = ?;

        """, (user_id, shipment_id))

        result = cursor.fetchone()

        # Synchronise subscription and shipment-level job atomically.
        # The helper uses this connection and does not commit or close it.
        from database.monitoring_job_repository import (
            _sync_monitoring_job_in_transaction,
        )
        _sync_monitoring_job_in_transaction(connection, shipment_id)

        connection.commit()
        return result[0] if result else None

    except Exception:

        connection.rollback()

        raise

    finally:

        connection.close()



def get_user_shipment(user_id, shipment_id):
    """
    Retrieve a specific user's shipment subscription.
    """

    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            SELECT *
            FROM user_shipments
            WHERE user_id = ?
            AND shipment_id = ?;
        """, (user_id, shipment_id))

        result = cursor.fetchone()

        if result:
            columns = [
                description[0]
                for description in cursor.description
            ]

            return dict(zip(columns, result))

        return None

    finally:
        connection.close()



def get_user_shipments(user_id):
    """
    Retrieve all shipment subscriptions belonging to a user.
    """

    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            SELECT
                us.*,
                s.tracking_number,
                s.carrier,
                s.current_status

            FROM user_shipments us

            INNER JOIN shipments s
                ON us.shipment_id = s.shipment_id

            WHERE us.user_id = ?

            ORDER BY us.created_at DESC;

        """, (user_id,))

        results = cursor.fetchall()

        columns = [
            description[0]
            for description in cursor.description
        ]

        return [
            dict(zip(columns, row))
            for row in results
        ]

    finally:
        connection.close()
