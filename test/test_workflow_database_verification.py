
import os
import tempfile
from pathlib import Path
from datetime import datetime
from unittest.mock import patch


def run_test():
    previous_db_path = os.environ.get("INDIAPOST_DB_PATH")

    try:
        with tempfile.TemporaryDirectory(
            prefix="IndiaPost_WorkflowDB_Test_"
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
            from tracking.shipment_workflow import track_and_save_shipment

            # Create a synthetic user.
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
                    ("TEST_WORKFLOW_DB_USER_001", now),
                )

                user_id = cursor.lastrowid
                connection.commit()

            finally:
                connection.close()

            print("\nTemporary database:", test_db_path)
            print("Synthetic User ID:", user_id)

            # Simulated tracking response.
            test_tracking_data = {
                "tracking_number": "TEST_WORKFLOW_DB_001IN",
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
                        "message": "Item dispatched",
                    }
                ],
            }

            # Execute the actual workflow while mocking external API calls.
            with patch(
                "tracking.shipment_workflow.find_tracking_id",
                return_value="TEST_AFTERSHIP_WORKFLOW_ID",
            ):
                with patch(
                    "tracking.shipment_workflow.track_shipment",
                    return_value=test_tracking_data,
                ):
                    workflow_result = track_and_save_shipment(
                        consignment_number="TEST_WORKFLOW_DB_001IN",
                        user_id=user_id,
                        monitoring_interval_hours=2,
                    )

            assert workflow_result, (
                "Shipment workflow did not return a result"
            )

            shipment_id = workflow_result["shipment_id"]

            print("Synthetic Shipment ID:", shipment_id)

            # Verify tracking-check and checkpoint persistence.
            connection = get_db_connection()

            try:
                cursor = connection.cursor()

                cursor.execute(
                    """
                    SELECT
                        check_id,
                        shipment_id,
                        check_status,
                        retrieved_status
                    FROM tracking_checks
                    WHERE shipment_id = ?
                    ORDER BY check_id DESC
                    LIMIT 1
                    """,
                    (shipment_id,),
                )

                check = cursor.fetchone()

                print("\nTRACKING CHECK VERIFICATION")

                assert check is not None, (
                    "Tracking check was not saved"
                )

                check_id = check[0]

                print("Check ID:", check_id)
                print("Shipment ID:", check[1])
                print("Check Status:", check[2])
                print("Retrieved Status:", check[3])

                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM checkpoints
                    WHERE shipment_id = ?
                    """,
                    (shipment_id,),
                )

                checkpoint_count = cursor.fetchone()[0]

                print("\nCHECKPOINT VERIFICATION")
                print("Shipment ID:", shipment_id)
                print("Total Stored Checkpoints:", checkpoint_count)

                # Verify the shipment record itself.
                cursor.execute(
                    """
                    SELECT current_status
                    FROM shipments
                    WHERE shipment_id = ?
                    """,
                    (shipment_id,),
                )

                shipment = cursor.fetchone()

                assert shipment is not None, (
                    "Shipment record was not saved"
                )

                assert shipment[0] == "In Transit", (
                    f"Unexpected shipment status: {shipment[0]}"
                )

                # Final verification.
                assert check[2] == "success", (
                    f"Unexpected tracking-check status: {check[2]}"
                )

                assert check[3] == "In Transit", (
                    f"Unexpected retrieved status: {check[3]}"
                )

                assert checkpoint_count == 1, (
                    f"Expected 1 checkpoint, found {checkpoint_count}"
                )

                print("\nDATABASE INTEGRATION VERIFICATION: PASSED")

            finally:
                connection.close()

    finally:
        if previous_db_path is None:
            os.environ.pop("INDIAPOST_DB_PATH", None)
        else:
            os.environ["INDIAPOST_DB_PATH"] = previous_db_path


if __name__ == "__main__":
    run_test()
