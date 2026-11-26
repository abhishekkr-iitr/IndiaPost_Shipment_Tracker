from datetime import datetime

from database.db_connection import get_db_connection


def save_shipment_in_transaction(
    connection,
    tracking_data,
    provider_tracking_id=None,
):
    """
    Insert or update a shipment using a caller-provided connection.

    This function does not commit, roll back, or close the connection.
    The caller controls the surrounding transaction.

    Existing shipment information is updated using the same behaviour
    as the original save_shipment() implementation.

    The provider-event ordering watermark is deliberately preserved.
    Webhook-specific timestamp ordering is handled separately.
    """

    if connection is None:
        raise ValueError("A database connection is required.")

    if not isinstance(tracking_data, dict):
        raise ValueError("Tracking data must be a dictionary.")

    current_time = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )

    cursor = connection.cursor()

    cursor.execute(
        """
        INSERT INTO shipments (
            tracking_number,
            carrier,
            provider,
            provider_tracking_id,
            current_status,
            status_message,
            origin,
            destination,
            pickup_date,
            delivery_date,
            last_checked_at,
            last_status_change_at,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

        ON CONFLICT(tracking_number, carrier)

        DO UPDATE SET

            provider = excluded.provider,

            provider_tracking_id = COALESCE(
                excluded.provider_tracking_id,
                shipments.provider_tracking_id
            ),

            current_status = excluded.current_status,

            status_message = excluded.status_message,

            origin = excluded.origin,

            destination = excluded.destination,

            pickup_date = excluded.pickup_date,

            delivery_date = excluded.delivery_date,

            last_checked_at = excluded.last_checked_at,

            last_status_change_at =
                CASE
                    WHEN shipments.current_status
                         IS NOT excluded.current_status
                    THEN excluded.last_checked_at
                    ELSE shipments.last_status_change_at
                END,

            updated_at = excluded.updated_at;
        """,
        (
            tracking_data.get("tracking_number"),
            tracking_data.get("carrier"),
            "aftership",
            provider_tracking_id,
            tracking_data.get("current_status"),
            tracking_data.get("status_message"),
            tracking_data.get("origin"),
            tracking_data.get("destination"),
            tracking_data.get("pickup_date"),
            tracking_data.get("delivery_date"),
            current_time,
            None,
            current_time,
            current_time,
        ),
    )

    cursor.execute(
        """
        SELECT shipment_id
        FROM shipments
        WHERE tracking_number = ?
          AND carrier = ?;
        """,
        (
            tracking_data.get("tracking_number"),
            tracking_data.get("carrier"),
        ),
    )

    result = cursor.fetchone()

    return result[0] if result else None


def save_shipment(tracking_data, provider_tracking_id=None):
    """
    Insert a new shipment or update an existing shipment.

    Backward-compatible public interface.

    Opens its own database connection, commits the operation,
    and closes the connection.
    """

    connection = get_db_connection()

    try:
        shipment_id = save_shipment_in_transaction(
            connection=connection,
            tracking_data=tracking_data,
            provider_tracking_id=provider_tracking_id,
        )

        connection.commit()

        return shipment_id

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


def get_shipment_by_tracking_number(
    tracking_number,
    carrier="india-post",
):
    """
    Retrieve an existing shipment using its tracking number
    and carrier.
    """

    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            SELECT *
            FROM shipments
            WHERE tracking_number = ?
              AND carrier = ?;
            """,
            (tracking_number, carrier),
        )

        result = cursor.fetchone()

        if result:
            columns = [
                description[0]
                for description in cursor.description
            ]

            shipment = dict(zip(columns, result))

            return shipment

        return None

    finally:
        connection.close()