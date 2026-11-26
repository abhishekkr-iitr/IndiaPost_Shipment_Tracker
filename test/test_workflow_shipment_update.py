
import os
import tempfile
from pathlib import Path
from datetime import datetime


def run_test():

    # Create a separate temporary database for this test.
    with tempfile.TemporaryDirectory(
        prefix="IndiaPost_Shipment_Update_Test_"
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

            # Create a synthetic user required by the foreign key.
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
                    "TEST_WORKFLOW_USER",
                    datetime.now().astimezone().isoformat(
                        timespec="seconds"
                    )
                ))

                user_id = cursor.lastrowid

                connection.commit()

            finally:

                connection.close()

            print("Synthetic Test User ID:", user_id)

            # Import workflow and repositories after selecting
            # the temporary database.
            from tracking.shipment_workflow import track_and_save_shipment

            from database.shipment_repository import (
                get_shipment_by_tracking_number
            )

            from database.user_shipment_repository import (
                get_user_shipment
            )

            from unittest.mock import patch

            # Simulated updated tracking information.
            updated_tracking_data = {
                "tracking_number": "TEST456789123IN",
                "carrier": "india-post",
                "current_status": "Out for Delivery",
                "status_message": "Item out for delivery",
                "origin": "TEST ORIGIN",
                "destination": "TEST DESTINATION",
                "pickup_date": "2026-11-12T09:00:00+05:30",
                "delivery_date": None,
                "checkpoints": [
                    {
                        "date": "2026-11-12T11:00:00+05:30",
                        "location": "TEST DELIVERY OFFICE",
                        "status": "Out for Delivery",
                        "message": "Item out for delivery"
                    }
                ]
            }

            # Mock AfterShip interactions.
            with patch(
                "tracking.shipment_workflow.find_tracking_id",
                return_value="TEST_AFTERSHIP_SUBSCRIPTION_ID"
            ):

                with patch(
                    "tracking.shipment_workflow.track_shipment",
                    return_value=updated_tracking_data
                ):

                    result = track_and_save_shipment(
                        consignment_number="TEST456789123IN",
                        user_id=user_id,
                        monitoring_interval_hours=2
                    )

            # Verify workflow execution.
            assert result is not None, "Workflow failed."

            shipment_id = result["shipment_id"]

            assert result["user_shipment_id"] is not None
            assert result["inserted_checkpoints"] == 1

            # Verify updated shipment information.
            shipment = get_shipment_by_tracking_number(
                "TEST456789123IN"
            )

            assert shipment is not None
            assert shipment["current_status"] == "Out for Delivery"

            # Verify user subscription.
            subscription = get_user_shipment(
                user_id=user_id,
                shipment_id=shipment_id
            )

            assert subscription is not None
            assert subscription["monitoring_enabled"] == 1
            assert subscription["monitoring_interval_hours"] == 2

            print("\nSHIPMENT UPDATE INTEGRATION TEST: PASSED")

            print("Shipment ID:", shipment_id)
            print("Updated Status:", shipment["current_status"])
            print("New Checkpoints Inserted:", result["inserted_checkpoints"])

            print("User Shipment ID:", result["user_shipment_id"])
            print("Monitoring Enabled:", subscription["monitoring_enabled"])
            print("Monitoring Interval:", subscription["monitoring_interval_hours"])

        finally:

            # Restore the previous database setting.
            if previous_database_path is None:
                os.environ.pop("INDIAPOST_DB_PATH", None)
            else:
                os.environ["INDIAPOST_DB_PATH"] = previous_database_path


if __name__ == "__main__":
    run_test()
