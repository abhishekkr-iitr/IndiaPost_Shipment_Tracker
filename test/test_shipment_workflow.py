
import os
import tempfile
from pathlib import Path
from unittest.mock import patch


def run_test():
    original_db_path = os.environ.get("INDIAPOST_DB_PATH")

    try:
        with tempfile.TemporaryDirectory(
            prefix="IndiaPost_Shipment_Workflow_Test_"
        ) as temp_dir:

            test_db_path = Path(temp_dir) / "isolated_test.db"
            os.environ["INDIAPOST_DB_PATH"] = str(test_db_path)

            # Import after selecting the temporary database.
            from database.schema import (
                create_users_table,
                create_shipments_table,
                create_checkpoints_table,
                create_tracking_checks_table,
                create_user_shipments_table,
                create_notification_preferences_table,
                create_notifications_table,
            )
            from database.db_connection import get_db_connection
            from tracking.shipment_workflow import track_and_save_shipment
            from database.shipment_repository import (
                get_shipment_by_tracking_number,
            )

            # Initialize the complete schema.
            create_users_table()
            create_shipments_table()
            create_checkpoints_table()
            create_tracking_checks_table()
            create_user_shipments_table()
            create_notification_preferences_table()
            create_notifications_table()

            print("All database tables created.")
            print("Temporary database:", test_db_path)

            test_tracking_data = {
                "tracking_number": "TEST987654321IN",
                "carrier": "india-post",
                "current_status": "In Transit",
                "status_message": "Shipment is in transit",
                "origin": "NEW DELHI, INDIA",
                "destination": "BENGALURU, INDIA",
                "pickup_date": "2026-11-10T10:00:00+05:30",
                "delivery_date": None,
                "checkpoints": [
                    {
                        "date": "2026-11-11T15:30:00+05:30",
                        "location": "NEW DELHI, INDIA",
                        "status": "In Transit",
                        "message": "Shipment dispatched",
                    }
                ],
            }

            mock_aftership_id = "TEST_AFTERSHIP_ID_001"

            # Mock both external tracking operations.
            with patch(
                "tracking.shipment_workflow.find_tracking_id",
                return_value=mock_aftership_id,
            ) as mock_find:

                with patch(
                    "tracking.shipment_workflow.track_shipment",
                    return_value=test_tracking_data,
                ) as mock_track:

                    result = track_and_save_shipment(
                        "TEST987654321IN"
                    )

            assert result is not None, (
                "Shipment workflow returned no result."
            )

            assert result["shipment_id"] is not None
            assert result["tracking_data"]["current_status"] == "In Transit"

            mock_find.assert_called_once_with("TEST987654321IN")
            mock_track.assert_called_once_with(
                "TEST987654321IN",
                tracking_id=mock_aftership_id,
            )

            print("\nShipment Workflow Test: PASSED")
            print("Database Shipment ID:", result["shipment_id"])
            print("AfterShip Tracking ID:", mock_aftership_id)
            print(
                "Current Status:",
                result["tracking_data"]["current_status"],
            )

            # Verify the actual saved database record.
            saved_shipment = get_shipment_by_tracking_number(
                "TEST987654321IN"
            )

            assert saved_shipment is not None, (
                "Shipment was not found in the database."
            )

            assert saved_shipment["shipment_id"] == result["shipment_id"]
            assert saved_shipment["tracking_number"] == "TEST987654321IN"
            assert saved_shipment["carrier"] == "india-post"
            assert saved_shipment["provider"] == "aftership"
            assert (
                saved_shipment["provider_tracking_id"]
                == mock_aftership_id
            )
            assert saved_shipment["current_status"] == "In Transit"

            print("\nDatabase Verification: PASSED")
            print(
                "Consignment Number:",
                saved_shipment["tracking_number"],
            )
            print("Carrier:", saved_shipment["carrier"])
            print("Provider:", saved_shipment["provider"])
            print(
                "Current Status:",
                saved_shipment["current_status"],
            )
            print(
                "Stored AfterShip Tracking ID:",
                saved_shipment["provider_tracking_id"],
            )

            # Verify that the checkpoint was persisted too.
            connection = get_db_connection()

            try:
                cursor = connection.cursor()

                cursor.execute("""
                    SELECT COUNT(*)
                    FROM checkpoints
                    WHERE shipment_id = ?;
                """, (result["shipment_id"],))

                checkpoint_count = cursor.fetchone()[0]

                assert checkpoint_count == 1, (
                    f"Expected 1 checkpoint, found {checkpoint_count}."
                )

                print("Stored Checkpoint Count:", checkpoint_count)

            finally:
                connection.close()

            print("\nSHIPMENT WORKFLOW TEST: PASSED")

    finally:
        if original_db_path is None:
            os.environ.pop("INDIAPOST_DB_PATH", None)
        else:
            os.environ["INDIAPOST_DB_PATH"] = original_db_path


if __name__ == "__main__":
    run_test()
