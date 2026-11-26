
import os
import tempfile
from pathlib import Path


def run_test():
    original_db_path = os.environ.get("INDIAPOST_DB_PATH")

    try:
        with tempfile.TemporaryDirectory(
            prefix="IndiaPost_Missing_Shipment_Test_"
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
            from database.shipment_repository import (
                get_shipment_by_tracking_number,
            )

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

            # Confirm that the temporary shipments table is empty.
            connection = get_db_connection()

            try:
                cursor = connection.cursor()
                cursor.execute("SELECT COUNT(*) FROM shipments")
                initial_count = cursor.fetchone()[0]

                assert initial_count == 0, (
                    "Expected an empty shipments table."
                )

            finally:
                connection.close()

            # Search for a shipment that does not exist.
            shipment = get_shipment_by_tracking_number(
                "DUMMY000000000IN"
            )

            assert shipment is None, (
                "Unexpected shipment record was found."
            )

            print("\nMissing Shipment Test: PASSED")
            print("No shipment record was found.")
            print(
                "Application handled the missing record correctly."
            )
            print("\nMISSING SHIPMENT TEST: PASSED")

    finally:
        # Restore the original database setting.
        if original_db_path is None:
            os.environ.pop("INDIAPOST_DB_PATH", None)
        else:
            os.environ["INDIAPOST_DB_PATH"] = original_db_path


if __name__ == "__main__":
    run_test()
