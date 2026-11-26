
import os
import tempfile
from pathlib import Path
from datetime import datetime


def run_test():
    previous_db_path = os.environ.get("INDIAPOST_DB_PATH")

    try:
        with tempfile.TemporaryDirectory(
            prefix="IndiaPost_GetShipment_Test_"
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
            from database.shipment_repository import (
                get_shipment_by_tracking_number,
            )

            # Create a synthetic shipment.
            now = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )

            connection = get_db_connection()

            try:
                cursor = connection.cursor()

                cursor.execute(
                    """
                    INSERT INTO shipments (
                        tracking_number,
                        carrier,
                        provider,
                        current_status,
                        status_message,
                        origin,
                        destination,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "TEST123456789IN",
                        "india-post",
                        "aftership",
                        "In Transit",
                        "Shipment is moving through the network",
                        "Delhi, India",
                        "Bengaluru, India",
                        now,
                        now,
                    ),
                )

                shipment_id = cursor.lastrowid
                connection.commit()

            finally:
                connection.close()

            print("\nTemporary database:", test_db_path)
            print("Synthetic Shipment ID:", shipment_id)

            # Retrieve the shipment using the actual repository.
            shipment = get_shipment_by_tracking_number(
                "TEST123456789IN"
            )

            assert shipment is not None, (
                "Shipment was not found"
            )

            print("\nShipment Retrieved Successfully!\n")

            print("Shipment ID:", shipment["shipment_id"])
            print("Consignment Number:", shipment["tracking_number"])
            print("Carrier:", shipment["carrier"])
            print("Current Status:", shipment["current_status"])
            print("Status Message:", shipment["status_message"])
            print("Origin:", shipment["origin"])
            print("Destination:", shipment["destination"])

            # Verify the returned fields.
            assert shipment["shipment_id"] == shipment_id
            assert shipment["tracking_number"] == "TEST123456789IN"
            assert shipment["carrier"] == "india-post"
            assert shipment["current_status"] == "In Transit"
            assert (
                shipment["status_message"]
                == "Shipment is moving through the network"
            )
            assert shipment["origin"] == "Delhi, India"
            assert shipment["destination"] == "Bengaluru, India"

            # Verify that an unknown tracking number returns no record.
            missing_shipment = get_shipment_by_tracking_number(
                "NONEXISTENT123IN"
            )

            assert missing_shipment is None, (
                "Unexpected shipment returned for an unknown tracking number"
            )

            print("\nMissing Shipment Handling: PASSED")
            print("\nGET SHIPMENT REPOSITORY TEST: PASSED")

    finally:
        if previous_db_path is None:
            os.environ.pop("INDIAPOST_DB_PATH", None)
        else:
            os.environ["INDIAPOST_DB_PATH"] = previous_db_path


if __name__ == "__main__":
    run_test()
