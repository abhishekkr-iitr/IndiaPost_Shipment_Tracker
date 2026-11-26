
import os
import tempfile
from pathlib import Path
from datetime import datetime


def run_test():
    original_db_path = os.environ.get("INDIAPOST_DB_PATH")

    try:
        with tempfile.TemporaryDirectory(
            prefix="IndiaPost_Update_Shipment_Test_"
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
            from database.shipment_repository import save_shipment

            # Initialize the complete database schema.
            create_users_table()
            create_shipments_table()
            create_checkpoints_table()
            create_tracking_checks_table()
            create_user_shipments_table()
            create_notification_preferences_table()
            create_notifications_table()

            print("All database tables created.")
            print("Temporary database:", test_db_path)

            initial_tracking_data = {
                "tracking_number": "TEST123456789IN",
                "carrier": "india-post",
                "current_status": "In Transit",
                "status_message": "Shipment is in transit",
                "origin": "NEW DELHI, INDIA",
                "destination": "BENGALURU, INDIA",
                "pickup_date": "2026-11-10T10:00:00+05:30",
                "delivery_date": None,
                "checkpoints": [],
            }

            updated_tracking_data = {
                "tracking_number": "TEST123456789IN",
                "carrier": "india-post",
                "current_status": "Out for Delivery",
                "status_message": "Shipment is out for delivery",
                "origin": "NEW DELHI, INDIA",
                "destination": "BENGALURU, INDIA",
                "pickup_date": "2026-11-10T10:00:00+05:30",
                "delivery_date": None,
                "checkpoints": [],
            }

            # Step 1: Insert the initial shipment.
            initial_shipment_id = save_shipment(
                initial_tracking_data
            )

            assert initial_shipment_id is not None, (
                "Initial shipment insertion failed."
            )

            print("\nInitial shipment saved.")
            print("Database Shipment ID:", initial_shipment_id)

            # Capture the original status-change timestamp.
            connection = get_db_connection()

            try:
                cursor = connection.cursor()

                cursor.execute("""
                    SELECT last_status_change_at
                    FROM shipments
                    WHERE tracking_number = ?
                    AND carrier = ?;
                """, (
                    "TEST123456789IN",
                    "india-post",
                ))

                initial_timestamp = cursor.fetchone()[0]

            finally:
                connection.close()

            # Step 2: Update the existing shipment.
            shipment_id = save_shipment(
                updated_tracking_data
            )

            assert shipment_id == initial_shipment_id, (
                "Shipment ID changed during update."
            )

            print("\nShipment Update Test")
            print("Shipment updated successfully!")
            print("Database Shipment ID:", shipment_id)

            # Step 3: Verify the updated database record.
            connection = get_db_connection()

            try:
                cursor = connection.cursor()

                cursor.execute("""
                    SELECT
                        shipment_id,
                        tracking_number,
                        current_status,
                        last_status_change_at
                    FROM shipments
                    WHERE tracking_number = ?
                    AND carrier = ?;
                """, (
                    "TEST123456789IN",
                    "india-post",
                ))

                result = cursor.fetchone()

                assert result is not None, (
                    "Updated shipment was not found."
                )

                assert result[0] == initial_shipment_id
                assert result[2] == "Out for Delivery"

                updated_timestamp = result[3]

                assert updated_timestamp is not None, (
                    "Status-change timestamp was not recorded."
                )

                # Confirm that the timestamp is valid ISO format.
                datetime.fromisoformat(updated_timestamp)

                if initial_timestamp is not None:
                    assert updated_timestamp != initial_timestamp, (
                        "Status-change timestamp did not change."
                    )

                print("\nUpdated Database Information")
                print("Shipment ID:", result[0])
                print("Consignment Number:", result[1])
                print("Current Status:", result[2])
                print("Last Status Change:", result[3])

                # Step 4: Verify duplicate prevention.
                cursor.execute("""
                    SELECT COUNT(*)
                    FROM shipments
                    WHERE tracking_number = ?
                    AND carrier = ?;
                """, (
                    "TEST123456789IN",
                    "india-post",
                ))

                record_count = cursor.fetchone()[0]

                print(
                    "\nTotal Records for Test Shipment:",
                    record_count,
                )

                assert record_count == 1, (
                    "Duplicate shipment records were created."
                )

                print("Duplicate Prevention Test: PASSED")

            finally:
                connection.close()

            print("\nSHIPMENT UPDATE TEST: PASSED")

    finally:
        # Restore the original database setting.
        if original_db_path is None:
            os.environ.pop("INDIAPOST_DB_PATH", None)
        else:
            os.environ["INDIAPOST_DB_PATH"] = original_db_path


if __name__ == "__main__":
    run_test()
