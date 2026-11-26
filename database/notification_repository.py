from datetime import datetime

from database.db_connection import get_db_connection


def create_notification(
    user_shipment_id=None,
    notification_type=None,
    recipient=None,
    idempotency_key=None,
    checkpoint_id=None,
    previous_status=None,
    new_status=None,
    notification_message=None,
    guest_subscription_id=None,
):
    """
    Create a new notification record for a registered user or a guest.

    Prevent duplicate notifications using an idempotency key.
    Enforces that exactly one of user_shipment_id or guest_subscription_id is provided.
    """

    # Step 1: Validate mandatory inputs

    if (user_shipment_id is None and guest_subscription_id is None) or (
        user_shipment_id is not None and guest_subscription_id is not None
    ):
        raise ValueError("Exactly one of user_shipment_id or guest_subscription_id must be provided.")

    if notification_type not in ("telegram", "email", "sms", "whatsapp"):
        raise ValueError(
            "Notification type must be telegram, email, sms, or whatsapp."
        )

    if not recipient:
        raise ValueError("Notification recipient is required.")

    if not idempotency_key:
        raise ValueError("Idempotency key is required.")

    # Step 2: Generate current timestamp

    current_time = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )

    # Step 3: Connect to database

    connection = get_db_connection()

    try:

        cursor = connection.cursor()

        # Step 4: Insert notification.
        # Check if guest_subscription_id column exists
        cols = {row[1] for row in cursor.execute('PRAGMA table_info("notifications")').fetchall()}

        if "guest_subscription_id" in cols:
            cursor.execute("""
                INSERT INTO notifications (
                    user_shipment_id,
                    guest_subscription_id,
                    checkpoint_id,
                    notification_type,
                    recipient,
                    previous_status,
                    new_status,
                    notification_message,
                    delivery_status,
                    idempotency_key,
                    attempt_count,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, 0, ?)
                ON CONFLICT(idempotency_key) DO NOTHING;
            """, (
                user_shipment_id,
                guest_subscription_id,
                checkpoint_id,
                notification_type,
                recipient,
                previous_status,
                new_status,
                notification_message,
                idempotency_key,
                current_time
            ))
        else:
            cursor.execute("""
                INSERT INTO notifications (
                    user_shipment_id,
                    checkpoint_id,
                    notification_type,
                    recipient,
                    previous_status,
                    new_status,
                    notification_message,
                    delivery_status,
                    idempotency_key,
                    attempt_count,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, 0, ?)
                ON CONFLICT(idempotency_key) DO NOTHING;
            """, (
                user_shipment_id,
                checkpoint_id,
                notification_type,
                recipient,
                previous_status,
                new_status,
                notification_message,
                idempotency_key,
                current_time
            ))

        connection.commit()

        # Step 5: Retrieve notification ID

        cursor.execute("""
            SELECT notification_id
            FROM notifications
            WHERE idempotency_key = ?;
        """, (idempotency_key,))

        result = cursor.fetchone()

        return result[0] if result else None

    except Exception:

        connection.rollback()

        raise

    finally:

        connection.close()



def get_notification_by_idempotency_key(idempotency_key):
    """
    Retrieve an existing notification using its idempotency key.
    """

    if not idempotency_key:
        raise ValueError("Idempotency key is required.")

    connection = get_db_connection()

    try:

        cursor = connection.cursor()

        cursor.execute("""
            SELECT *
            FROM notifications
            WHERE idempotency_key = ?;
        """, (idempotency_key,))

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



def get_pending_notifications(limit=100):
    """
    Retrieve notifications waiting for delivery.
    """

    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("Limit must be a positive integer.")

    connection = get_db_connection()

    try:

        cursor = connection.cursor()

        cursor.execute("""
            SELECT *
            FROM notifications
            WHERE delivery_status = 'pending'
            ORDER BY created_at ASC
            LIMIT ?;
        """, (limit,))

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



def update_notification_status(
    notification_id,
    delivery_status,
    provider_message_id=None,
    error_message=None
):
    """
    Update the delivery status of a notification.
    """

    # Step 1: Validate inputs

    if not notification_id:
        raise ValueError("Notification ID is required.")

    if delivery_status not in ("pending", "sent", "failed"):
        raise ValueError(
            "Delivery status must be pending, sent, or failed."
        )

    # Step 2: Generate current timestamp

    current_time = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )

    connection = get_db_connection()

    try:

        cursor = connection.cursor()

        # Step 3: Update notification record

        cursor.execute("""
            UPDATE notifications

            SET
                delivery_status = ?,

                provider_message_id = COALESCE(
                    ?,
                    provider_message_id
                ),

                sent_at = CASE
                    WHEN ? = 'sent' THEN ?
                    ELSE sent_at
                END,

                error_message = ?

            WHERE notification_id = ?;

        """, (
            delivery_status,
            provider_message_id,
            delivery_status,
            current_time,
            error_message,
            notification_id
        ))

        if cursor.rowcount == 0:
            raise ValueError("Notification record not found.")

        connection.commit()

        return True

    except Exception:

        connection.rollback()

        raise

    finally:

        connection.close()



def record_notification_attempt(
    notification_id,
    error_message=None
):
    """
    Record a notification delivery attempt.

    Increment the attempt count and update the latest attempt time.
    """

    if not notification_id:
        raise ValueError("Notification ID is required.")

    current_time = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )

    connection = get_db_connection()

    try:

        cursor = connection.cursor()

        cursor.execute("""
            UPDATE notifications

            SET
                attempt_count = attempt_count + 1,
                last_attempt_at = ?,
                error_message = ?

            WHERE notification_id = ?;

        """, (
            current_time,
            error_message,
            notification_id
        ))

        if cursor.rowcount == 0:
            raise ValueError("Notification record not found.")

        connection.commit()

        return True

    except Exception:

        connection.rollback()

        raise

    finally:

        connection.close()
