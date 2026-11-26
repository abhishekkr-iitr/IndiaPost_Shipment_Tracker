
import os
import tempfile
from pathlib import Path


def run_test():
    original_db_path = os.environ.get("INDIAPOST_DB_PATH")

    try:
        with tempfile.TemporaryDirectory(
            prefix="IndiaPost_Save_Shipment_Test_"
        ) as temp_dir:

            test_db_path = Path(temp_dir) / "isolated_test.db"
            os.environ["INDIAPOST_DB_PATH"] = str(test_db_path)

            # Import only after selecting the isolated database.
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

            test_tracking_data = {
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

            # First save: expect a new shipment.
            shipment_id = save_shipment(test_tracking_data)

            assert shipment_id is not None, (
                "Shipment insertion failed."
            )

            print("\nShipment saved successfully!")
            print("Database Shipment ID:", shipment_id)

            # Verify the saved shipment using the same database.
            connection = get_db_connection()

            try:
                cursor = connection.cursor()

                cursor.execute("""
                    SELECT
                        shipment_id,
                        tracking_number,
                        carrier,
                        provider,
                        current_status,
                        origin,
                        destination
                    FROM shipments
                    WHERE tracking_number = ?
                    AND carrier = ?;
                """, (
                    "TEST123456789IN",
                    "india-post",
                ))

                result = cursor.fetchone()

                assert result is not None, (
                    "Database retrieval failed."
                )

                assert result[0] == shipment_id
                assert result[1] == "TEST123456789IN"
                assert result[2] == "india-post"
                assert result[3] == "aftership"
                assert result[4] == "In Transit"
                assert result[5] == "NEW DELHI, INDIA"
                assert result[6] == "BENGALURU, INDIA"

                print("\nDatabase Retrieval Successful!")
                print("Shipment ID:", result[0])
                print("Consignment Number:", result[1])
                print("Carrier:", result[2])
                print("Provider:", result[3])
                print("Current Status:", result[4])
                print("Origin:", result[5])
                print("Destination:", result[6])

            finally:
                connection.close()

            # Second save: same shipment, changed status.
            updated_tracking_data = dict(test_tracking_data)
            updated_tracking_data["current_status"] = "Out for Delivery"
            updated_tracking_data["status_message"] = (
                "Shipment is out for delivery"
            )

            second_shipment_id = save_shipment(
                updated_tracking_data
            )

            assert second_shipment_id == shipment_id, (
                "Duplicate shipment was created instead of updating "
                "the existing shipment."
            )

            connection = get_db_connection()

            try:
                cursor = connection.cursor()

                cursor.execute("""
                    SELECT shipment_id, current_status
                    FROM shipments
                    WHERE tracking_number = ?
                    AND carrier = ?;
                """, (
                    "TEST123456789IN",
                    "india-post",
                ))

                updated_result = cursor.fetchone()

                cursor.execute("""
                    SELECT COUNT(*)
                    FROM shipments
                    WHERE tracking_number = ?
                    AND carrier = ?;
                """, (
                    "TEST123456789IN",
                    "india-post",
                ))

                shipment_count = cursor.fetchone()[0]

                assert updated_result is not None
                assert updated_result[0] == shipment_id
                assert updated_result[1] == "Out for Delivery"
                assert shipment_count == 1

                print("\nDuplicate Prevention and Update: PASSED")
                print("Existing Shipment ID retained:", updated_result[0])
                print("Updated Status:", updated_result[1])
                print("Matching Shipment Records:", shipment_count)

            finally:
                connection.close()

            print("\nSAVE SHIPMENT REPOSITORY TEST: PASSED")

    finally:
        if original_db_path is None:
            os.environ.pop("INDIAPOST_DB_PATH", None)
        else:
            os.environ["INDIAPOST_DB_PATH"] = original_db_path


if __name__ == "__main__":
    run_test()
