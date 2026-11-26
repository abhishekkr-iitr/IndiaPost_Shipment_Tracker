from datetime import datetime

from database.db_connection import get_db_connection


def save_notification_preferences(
    user_id,
    email=None,
    mobile_number=None,
    email_enabled=None,
    sms_enabled=None,
    whatsapp_enabled=None
):
    """
    Insert or update notification preferences for a user.

    Existing values are preserved when optional arguments
    are not supplied.
    """

    if not user_id:
        raise ValueError("User ID is required.")

    connection = get_db_connection()

    try:

        cursor = connection.cursor()

        current_time = datetime.now().astimezone().isoformat(
            timespec="seconds"
        )

        cursor.execute("""
            INSERT INTO notification_preferences (

                user_id,
                email,
                mobile_number,
                email_enabled,
                sms_enabled,
                whatsapp_enabled,
                created_at,
                updated_at

            )

            VALUES (
                ?, ?, ?,
                COALESCE(?, 1),
                COALESCE(?, 0),
                COALESCE(?, 0),
                ?, ?
            )

            ON CONFLICT(user_id)

            DO UPDATE SET

                email = COALESCE(
                    excluded.email,
                    notification_preferences.email
                ),

                mobile_number = COALESCE(
                    excluded.mobile_number,
                    notification_preferences.mobile_number
                ),

                email_enabled = CASE
                    WHEN ? IS NOT NULL
                    THEN excluded.email_enabled
                    ELSE notification_preferences.email_enabled
                END,

                sms_enabled = CASE
                    WHEN ? IS NOT NULL
                    THEN excluded.sms_enabled
                    ELSE notification_preferences.sms_enabled
                END,

                whatsapp_enabled = CASE
                    WHEN ? IS NOT NULL
                    THEN excluded.whatsapp_enabled
                    ELSE notification_preferences.whatsapp_enabled
                END,

                updated_at = excluded.updated_at;

        """, (

            user_id,
            email,
            mobile_number,
            email_enabled,
            sms_enabled,
            whatsapp_enabled,
            current_time,
            current_time,

            email_enabled,
            sms_enabled,
            whatsapp_enabled

        ))

        connection.commit()

        return True

    except Exception:

        connection.rollback()

        raise

    finally:

        connection.close()


def get_notification_preferences(user_id):
    """
    Retrieve notification preferences for a specific user.
    """

    if not user_id:
        raise ValueError("User ID is required.")

    connection = get_db_connection()

    try:

        cursor = connection.cursor()

        cursor.execute("""
            SELECT *
            FROM notification_preferences
            WHERE user_id = ?;
        """, (user_id,))

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