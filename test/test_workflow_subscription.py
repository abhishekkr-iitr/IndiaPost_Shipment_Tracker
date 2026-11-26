
import os
import tempfile
from pathlib import Path
from datetime import datetime
from unittest.mock import patch


def run_test():

    # Create a separate temporary database for this test.
    with tempfile.TemporaryDirectory(
        prefix="IndiaPost_Subscription_Test_"
    ) as temp_folder:

        test_database = Path(temp_folder) / "test_shipments.db"

        previous_database_path = os.environ.get(
            "INDIAPOST_DB_PATH"
        )

        os.environ["INDIAPOST_DB_PATH"] = str(test_database)

        try:

            # Import schema functions after selecting the database.
            from database.schema import (
                create_users_table,
                create_shipments_table,
                create_user_shipments_table,
                create_checkpoints_table,
                create_tracking_checks_table,
                create_notification_preferences_table,
                create_notifications_table,
            )

            # Create all seven application tables.
            create_users_table()
            create_shipments_table()
            create_user_shipments_table()
            create_checkpoints_table()
            create_tracking_checks_table()
            create_notification_preferences_table()
            create_notifications_table()

            print("\nTemporary database created:", test_database)

            # Create a synthetic user for this test.
            from database.db_connection import get_db_connection

            connection = get_db_connection()

            try:

                cursor = connection.cursor()

                cursor.execute("""
                    INSERT INTO users (
                        external_user_id,
                        created_at
                    )
                    VALUES (?, ?);
                """, (
                    "TEST_SUBSCRIPTION_USER",
                    datetime.now().astimezone().isoformat(
                        timespec="seconds"
                    )
                ))

                user_id = cursor.lastrowid
                connection.commit()

            finally:

                connection.close()

            print("Synthetic Test User ID:", user_id)

            # Import workflow and repository functions.
            from tracking.shipment_workflow import (
                track_and_save_shipment
            )

            from database.user_shipment_repository import (
                get_user_shipment
            )

            # Synthetic tracking data for testing.
            test_tracking_data = {
                "tracking_number": "TEST456789123IN",
                "carrier": "india-post",
                "current_status": "In Transit",
                "status_message": "Item dispatched",
                "origin": "TEST ORIGIN",
                "destination": "TEST DESTINATION",
                "pickup_date": "2026-11-12T09:00:00+05:30",
                "delivery_date": None,
                "checkpoints": [
                    {
                        "date": "2026-11-12T10:00:00+05:30",
                        "location": "TEST SORTING OFFICE",
                        "status": "In Transit",
                        "message": "Item dispatched"
                    }
                ]
            }

            # Mock external AfterShip interactions.
            with patch(
                "tracking.shipment_workflow.find_tracking_id",
                return_value="TEST_AFTERSHIP_SUBSCRIPTION_ID"
            ):

                with patch(
                    "tracking.shipment_workflow.track_shipment",
                    return_value=test_tracking_data
                ):

                    # First workflow execution.
                    first_result = track_and_save_shipment(
                        consignment_number="TEST456789123IN",
                        user_id=user_id,
                        monitoring_interval_hours=2
                    )

                    # Second execution with identical tracking data.
                    second_result = track_and_save_shipment(
                        consignment_number="TEST456789123IN",
                        user_id=user_id,
                        monitoring_interval_hours=2
                    )

            # Verify both workflow executions.
            assert first_result is not None
            assert second_result is not None

            # Both executions should use the same shipment.
            assert (
                first_result["shipment_id"]
                == second_result["shipment_id"]
            )

            # Both executions should use the same subscription.
            assert (
                first_result["user_shipment_id"]
                == second_result["user_shipment_id"]
            )

            # The second execution must not insert a duplicate checkpoint.
            assert second_result["inserted_checkpoints"] == 0

            # Verify subscription in the database.
            subscription = get_user_shipment(
                user_id=user_id,
                shipment_id=second_result["shipment_id"]
            )

            assert subscription is not None, (
                "Subscription was not found."
            )

            assert subscription["monitoring_enabled"] == 1

            assert (
                subscription["monitoring_interval_hours"] == 2
            )

            assert subscription["next_check_at"] is not None

            print(
                "\nINTEGRATED DUPLICATE PREVENTION TEST: PASSED"
            )

            print("First Shipment ID:", first_result["shipment_id"])
            print("Second Shipment ID:", second_result["shipment_id"])

            print(
                "First User Shipment ID:",
                first_result["user_shipment_id"]
            )

            print(
                "Second User Shipment ID:",
                second_result["user_shipment_id"]
            )

            print(
                "First Execution - New Checkpoints:",
                first_result["inserted_checkpoints"]
            )

            print(
                "Second Execution - New Checkpoints:",
                second_result["inserted_checkpoints"]
            )

            print(
                "Monitoring Enabled:",
                subscription["monitoring_enabled"]
            )

            print(
                "Monitoring Interval:",
                subscription["monitoring_interval_hours"]
            )

        finally:

            # Restore the previous database setting.
            if previous_database_path is None:
                os.environ.pop("INDIAPOST_DB_PATH", None)
            else:
                os.environ["INDIAPOST_DB_PATH"] = previous_database_path


if __name__ == "__main__":
    run_test()
