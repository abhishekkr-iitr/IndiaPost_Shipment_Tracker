
import os
import tempfile
from pathlib import Path


def run_test():

    # Create a temporary folder for this test's database.
    with tempfile.TemporaryDirectory(
        prefix="IndiaPost_Checkpoint_Test_"
    ) as temp_folder:

        test_database = Path(temp_folder) / "test_shipments.db"

        # Direct database connections to this temporary database.
        previous_database_path = os.environ.get(
            "INDIAPOST_DB_PATH"
        )

        os.environ["INDIAPOST_DB_PATH"] = str(test_database)

        try:

            # Import project modules after selecting the test database.
            from database.schema import (
                create_users_table,
                create_shipments_table,
                create_user_shipments_table,
                create_checkpoints_table,
                create_tracking_checks_table,
                create_notification_preferences_table,
                create_notifications_table,
            )

            from database.shipment_repository import save_shipment
            from database.checkpoint_repository import save_checkpoints
            from database.db_connection import get_db_connection

            # Create all seven application tables.
            create_users_table()
            create_shipments_table()
            create_user_shipments_table()
            create_checkpoints_table()
            create_tracking_checks_table()
            create_notification_preferences_table()
            create_notifications_table()

            print("\nTemporary database created:", test_database)

            # Step 1: Create a synthetic shipment.

            test_tracking_data = {
                "tracking_number": "TESTCHECKPOINT123IN",
                "carrier": "india-post",
                "current_status": "In Transit",
                "status_message": "Shipment is in transit",
                "origin": "NEW DELHI, INDIA",
                "destination": "BENGALURU, INDIA",
                "pickup_date": "2026-11-10T10:00:00+05:30",
                "delivery_date": None,
                "checkpoints": []
            }

            shipment_id = save_shipment(test_tracking_data)

            print("Test Shipment ID:", shipment_id)

            # Step 2: Define three synthetic checkpoints.

            test_checkpoints = [

                {
                    "date": "2026-11-10T10:00:00+05:30",
                    "location": "NEW DELHI, INDIA",
                    "status": "Picked Up",
                    "message": "Shipment picked up"
                },

                {
                    "date": "2026-11-10T15:00:00+05:30",
                    "location": "NEW DELHI, INDIA",
                    "status": "In Transit",
                    "message": "Shipment dispatched"
                },

                {
                    "date": "2026-11-10T23:00:00+05:30",
                    "location": "LUCKNOW, INDIA",
                    "status": "In Transit",
                    "message": "Shipment arrived at sorting facility"
                }

            ]

            # Step 3: Insert checkpoints for the first time.

            inserted_first_time = save_checkpoints(
                shipment_id,
                test_checkpoints
            )

            print("\nFirst Checkpoint Insertion")
            print("New Checkpoints Inserted:", inserted_first_time)

            # Step 4: Insert the same checkpoints again.

            inserted_second_time = save_checkpoints(
                shipment_id,
                test_checkpoints
            )

            print("\nDuplicate Prevention Test")
            print("New Checkpoints Inserted:", inserted_second_time)

            # Step 5: Retrieve saved checkpoints.

            connection = get_db_connection()

            try:

                cursor = connection.cursor()

                cursor.execute("""
                    SELECT
                        event_date,
                        location,
                        status,
                        message,
                        duration_since_previous_seconds

                    FROM checkpoints

                    WHERE shipment_id = ?

                    ORDER BY event_date;
                """, (shipment_id,))

                records = cursor.fetchall()

            finally:

                connection.close()

            print("\nSaved Checkpoint Information\n")

            for index, record in enumerate(records, start=1):

                print("Checkpoint:", index)
                print("Event Date:", record[0])
                print("Location:", record[1])
                print("Status:", record[2])
                print("Message:", record[3])
                print(
                    "Duration Since Previous (Seconds):",
                    record[4]
                )
                print()

            # Step 6: Verify the results.

            if (
                inserted_first_time == 3
                and inserted_second_time == 0
                and len(records) == 3
                and records[0][4] is None
                and records[1][4] == 18000
                and records[2][4] == 28800
            ):

                print("CHECKPOINT REPOSITORY TEST: PASSED")

            else:

                raise AssertionError(
                    "Checkpoint repository verification failed."
                )

        finally:

            # Restore the previous database setting.
            if previous_database_path is None:
                os.environ.pop("INDIAPOST_DB_PATH", None)
            else:
                os.environ["INDIAPOST_DB_PATH"] = previous_database_path


if __name__ == "__main__":
    run_test()
