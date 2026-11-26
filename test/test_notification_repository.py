
import os
import tempfile
from pathlib import Path
from datetime import datetime


def run_test():
    previous_db_path = os.environ.get("INDIAPOST_DB_PATH")

    try:
        with tempfile.TemporaryDirectory(
            prefix="IndiaPost_Notification_Test_"
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

            from database.notification_repository import (
                create_notification,
                get_notification_by_idempotency_key,
                get_pending_notifications,
                update_notification_status,
                record_notification_attempt,
            )

            # Create synthetic user, shipment and subscription.
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

                cursor.execute(
                    """
                    INSERT INTO shipments (
                        tracking_number,
                        carrier,
                        provider,
                        current_status,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "TEST987654321IN",
                        "india-post",
                        "aftership",
                        "In Transit",
                        now,
                        now,
                    ),
                )

                shipment_id = cursor.lastrowid

                cursor.execute(
                    """
                    INSERT INTO user_shipments (
                        user_id,
                        shipment_id,
                        created_at
                    )
                    VALUES (?, ?, ?)
                    """,
                    (user_id, shipment_id, now),
                )

                user_shipment_id = cursor.lastrowid
                connection.commit()

            finally:
                connection.close()

            print("\nTemporary database:", test_db_path)
            print("Synthetic User ID:", user_id)
            print("Synthetic Shipment ID:", shipment_id)
            print("User Shipment ID:", user_shipment_id)

            # TEST 1: Generate an idempotency key.
            idempotency_key = (
                "TEST_NOTIFICATION_EMAIL_"
                + datetime.now().strftime("%Y%m%d%H%M%S%f")
            )

            # TEST 2: Create a notification.
            notification_id = create_notification(
                user_shipment_id=user_shipment_id,
                notification_type="email",
                recipient="test@example.com",
                idempotency_key=idempotency_key,
                previous_status="In Transit",
                new_status="Out for Delivery",
                notification_message=(
                    "Your shipment is out for delivery."
                ),
            )

            assert notification_id is not None, (
                "Notification creation failed"
            )

            print("\nNotification Created!")
            print("Notification ID:", notification_id)

            # TEST 3: Test duplicate prevention.
            duplicate_notification_id = create_notification(
                user_shipment_id=user_shipment_id,
                notification_type="email",
                recipient="test@example.com",
                idempotency_key=idempotency_key,
                previous_status="In Transit",
                new_status="Out for Delivery",
                notification_message=(
                    "Your shipment is out for delivery."
                ),
            )

            print("\nDuplicate Prevention Test")
            print("Original Notification ID:", notification_id)
            print("Duplicate Request ID:", duplicate_notification_id)

            assert (
                notification_id == duplicate_notification_id
            ), "Duplicate notification was not prevented"

            # TEST 4: Retrieve by idempotency key.
            notification = get_notification_by_idempotency_key(
                idempotency_key
            )

            assert notification is not None, (
                "Notification could not be retrieved"
            )

            print("\nRetrieved Notification")
            print("Notification ID:", notification["notification_id"])
            print("Delivery Status:", notification["delivery_status"])
            print("Notification Type:", notification["notification_type"])

            # TEST 5: Retrieve pending notifications.
            pending_notifications = get_pending_notifications()

            pending_test_passed = any(
                item["notification_id"] == notification_id
                for item in pending_notifications
            )

            print(
                "\nPending Notifications Retrieved:",
                len(pending_notifications),
            )
            print("Test Notification Found:", pending_test_passed)

            assert pending_test_passed, (
                "New notification was not found in pending notifications"
            )

            # TEST 6: Record first delivery attempt (simulated failure).
            record_notification_attempt(
                notification_id,
                error_message=(
                    "Simulated email provider connection failure."
                ),
            )

            update_notification_status(
                notification_id,
                delivery_status="failed",
                error_message=(
                    "Simulated email provider connection failure."
                ),
            )

            print("\nFirst Delivery Attempt: FAILED (Simulated)")

            # TEST 7: Record second delivery attempt (simulated success).
            record_notification_attempt(notification_id)

            update_notification_status(
                notification_id,
                delivery_status="sent",
                provider_message_id="TEST_EMAIL_PROVIDER_001",
            )

            print("Second Delivery Attempt: SUCCESS (Simulated)")

            # TEST 8: Retrieve final notification record.
            final_notification = get_notification_by_idempotency_key(
                idempotency_key
            )

            assert final_notification is not None, (
                "Final notification record could not be retrieved"
            )

            print("\nFINAL NOTIFICATION RECORD\n")
            print(
                "Notification ID:",
                final_notification["notification_id"],
            )
            print(
                "Delivery Status:",
                final_notification["delivery_status"],
            )
            print(
                "Attempt Count:",
                final_notification["attempt_count"],
            )
            print(
                "Provider Message ID:",
                final_notification["provider_message_id"],
            )
            print("Sent At:", final_notification["sent_at"])
            print(
                "Last Attempt At:",
                final_notification["last_attempt_at"],
            )
            print(
                "Error Message:",
                final_notification["error_message"],
            )

            # TEST 9: Verify final results.
            assert (
                final_notification["delivery_status"] == "sent"
            ), "Final delivery status is not sent"

            assert (
                final_notification["attempt_count"] == 2
            ), "Expected exactly two delivery attempts"

            assert (
                final_notification["provider_message_id"]
                == "TEST_EMAIL_PROVIDER_001"
            ), "Unexpected provider message ID"

            assert (
                final_notification["sent_at"] is not None
            ), "Sent timestamp was not recorded"

            assert (
                final_notification["last_attempt_at"] is not None
            ), "Last attempt timestamp was not recorded"

            assert (
                final_notification["error_message"] is None
            ), "Error message was not cleared after successful delivery"

            print("\nNOTIFICATION REPOSITORY TEST: PASSED")

    finally:
        if previous_db_path is None:
            os.environ.pop("INDIAPOST_DB_PATH", None)
        else:
            os.environ["INDIAPOST_DB_PATH"] = previous_db_path


if __name__ == "__main__":
    run_test()
