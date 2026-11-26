
import os
import tempfile
from pathlib import Path


def run_test():

    # Create a separate temporary database for this test.
    with tempfile.TemporaryDirectory(
        prefix="IndiaPost_Late_Checkpoint_Test_"
    ) as temp_folder:

        test_database = Path(temp_folder) / "test_shipments.db"

        previous_database_path = os.environ.get(
            "INDIAPOST_DB_PATH"
        )

        os.environ["INDIAPOST_DB_PATH"] = str(test_database)

        try:

            # Import modules after selecting the temporary database.
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
                "tracking_number": "TESTLATE123456IN",
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

            # Step 2: Insert three initial checkpoints.

            initial_checkpoints = [

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

            initial_inserted_count = save_checkpoints(
                shipment_id,
                initial_checkpoints
            )

            print(
                "\nInitial Checkpoints Inserted:",
                initial_inserted_count
            )

            # Step 3: Introduce a late-arriving checkpoint.
            # Its event time is earlier than the second checkpoint.

            late_checkpoint = [

                {
                    "date": "2026-11-10T13:00:00+05:30",
                    "location": "NEW DELHI, INDIA",
                    "status": "In Transit",
                    "message": "Shipment processed at sorting facility"
                }

            ]

            inserted_count = save_checkpoints(
                shipment_id,
                late_checkpoint
            )

            print("\nLate Checkpoint Test")
            print("New Checkpoints Inserted:", inserted_count)

            # Step 4: Retrieve all checkpoints chronologically.

            connection = get_db_connection()

            try:

                cursor = connection.cursor()

                cursor.execute("""
                    SELECT
                        event_date,
                        message,
                        duration_since_previous_seconds

                    FROM checkpoints

                    WHERE shipment_id = ?

                    ORDER BY event_date;
                """, (shipment_id,))

                records = cursor.fetchall()

            finally:

                connection.close()

            print("\nUpdated Checkpoint Timeline\n")

            for index, record in enumerate(records, start=1):

                print("Checkpoint:", index)
                print("Event Date:", record[0])
                print("Message:", record[1])
                print("Duration (Seconds):", record[2])
                print()

            # Step 5: Validate the results.

            expected_durations = [
                None,
                10800,
                7200,
                28800
            ]

            actual_durations = [
                record[2] for record in records
            ]

            if (
                initial_inserted_count == 3
                and inserted_count == 1
                and len(records) == 4
                and actual_durations == expected_durations
            ):

                print("LATE CHECKPOINT TEST: PASSED")

            else:

                raise AssertionError(
                    "Late checkpoint verification failed. "
                    f"Expected durations: {expected_durations}; "
                    f"Actual durations: {actual_durations}; "
                    f"Initial insertions: {initial_inserted_count}; "
                    f"Late insertions: {inserted_count}"
                )

        finally:

            # Restore the previous database setting.
            if previous_database_path is None:
                os.environ.pop("INDIAPOST_DB_PATH", None)
            else:
                os.environ["INDIAPOST_DB_PATH"] = previous_database_path


if __name__ == "__main__":
    run_test()
