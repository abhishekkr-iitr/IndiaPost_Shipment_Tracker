
import os
import tempfile
from pathlib import Path
from datetime import datetime


def run_test():
    previous_db_path = os.environ.get("INDIAPOST_DB_PATH")

    try:
        with tempfile.TemporaryDirectory(
            prefix="IndiaPost_NotificationPreference_Test_"
        ) as temp_dir:

            test_db_path = Path(temp_dir) / "test_shipments.db"
            os.environ["INDIAPOST_DB_PATH"] = str(test_db_path)

            # Import after selecting the temporary database.
            from database.schema import (
                create_users_table,
                create_shipments_table,
                create_user_shipments_table,
                create_checkpoints_table,
                create_tracking_checks_table,
                create_notification_preferences_table,
                create_notifications_table,
            )

            create_users_table()
            create_shipments_table()
            create_user_shipments_table()
            create_checkpoints_table()
            create_tracking_checks_table()
            create_notification_preferences_table()
            create_notifications_table()

            from database.db_connection import get_db_connection
            from database.notification_preference_repository import (
                save_notification_preferences,
                get_notification_preferences,
            )

            # Create a fresh synthetic user.
            now = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )

            connection = get_db_connection()

            try:
                cursor = connection.cursor()

                cursor.execute(
                    """
                    INSERT INTO users (
                        external_user_id,
                        created_at
                    )
                    VALUES (?, ?)
                    """,
                    ("TEST_NOTIFICATION_USER_001", now),
                )

                user_id = cursor.lastrowid
                connection.commit()

            finally:
                connection.close()

            print("\nTemporary database:", test_db_path)
            print("Synthetic User ID:", user_id)

            # TEST 1: Save initial notification preferences.
            save_notification_preferences(
                user_id=user_id,
                email="test@example.com",
                mobile_number="+919999999999",
                email_enabled=1,
                sms_enabled=0,
                whatsapp_enabled=1,
            )

            print("\nInitial Notification Preferences Saved!")

            # TEST 2: Retrieve initial preferences.
            preferences = get_notification_preferences(user_id)

            assert preferences is not None, (
                "Initial notification preferences were not found"
            )

            assert preferences["email"] == "test@example.com"
            assert preferences["mobile_number"] == "+919999999999"
            assert preferences["email_enabled"] == 1
            assert preferences["sms_enabled"] == 0
            assert preferences["whatsapp_enabled"] == 1

            print("\nINITIAL PREFERENCES\n")
            print("Email:", preferences["email"])
            print("Mobile:", preferences["mobile_number"])
            print("Email Enabled:", preferences["email_enabled"])
            print("SMS Enabled:", preferences["sms_enabled"])
            print("WhatsApp Enabled:", preferences["whatsapp_enabled"])

            # TEST 3: Update only the SMS preference.
            save_notification_preferences(
                user_id=user_id,
                sms_enabled=1,
            )

            print("\nSMS Preference Updated!")

            # TEST 4: Retrieve and verify updated preferences.
            updated_preferences = get_notification_preferences(user_id)

            assert updated_preferences is not None, (
                "Updated notification preferences were not found"
            )

            print("\nUPDATED PREFERENCES\n")
            print("Email:", updated_preferences["email"])
            print("Mobile:", updated_preferences["mobile_number"])
            print("Email Enabled:", updated_preferences["email_enabled"])
            print("SMS Enabled:", updated_preferences["sms_enabled"])
            print("WhatsApp Enabled:", updated_preferences["whatsapp_enabled"])

            assert (
                updated_preferences["email"] == "test@example.com"
            ), "Email was unexpectedly changed"

            assert (
                updated_preferences["mobile_number"] == "+919999999999"
            ), "Mobile number was unexpectedly changed"

            assert (
                updated_preferences["email_enabled"] == 1
            ), "Email preference was unexpectedly changed"

            assert (
                updated_preferences["sms_enabled"] == 1
            ), "SMS preference was not updated"

            assert (
                updated_preferences["whatsapp_enabled"] == 1
            ), "WhatsApp preference was unexpectedly changed"

            print("\nNOTIFICATION PREFERENCE TEST: PASSED")

    finally:
        if previous_db_path is None:
            os.environ.pop("INDIAPOST_DB_PATH", None)
        else:
            os.environ["INDIAPOST_DB_PATH"] = previous_db_path


if __name__ == "__main__":
    run_test()
