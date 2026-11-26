
import os
import tempfile
from pathlib import Path
from datetime import datetime


def run_test():
    previous_db_path = os.environ.get("INDIAPOST_DB_PATH")

    try:
        with tempfile.TemporaryDirectory(
            prefix="IndiaPost_TrackingCheck_Test_"
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
            from database.tracking_check_repository import (
                start_tracking_check,
                complete_tracking_check,
            )

            # Create a synthetic shipment for this test.
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
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        "TEST_TRACKING_CHECK_001",
                        "india-post",
                        "aftership",
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

            # TEST 1: Successful tracking attempt
            print("\nTEST 1: Successful Tracking Attempt")

            check_id_1 = start_tracking_check(shipment_id)

            complete_tracking_check(
                check_id=check_id_1,
                check_status="success",
                retrieved_status="In Transit",
            )

            print("Successful tracking attempt recorded.")

            # TEST 2: Failed tracking attempt
            print("\nTEST 2: Failed Tracking Attempt")

            check_id_2 = start_tracking_check(shipment_id)

            complete_tracking_check(
                check_id=check_id_2,
                check_status="failed",
                error_message="Simulated API connection failure",
            )

            print("Failed tracking attempt recorded.")

            # TEST 3: Retrieve tracking history
            connection = get_db_connection()

            try:
                cursor = connection.cursor()

                cursor.execute(
                    """
                    SELECT
                        check_id,
                        shipment_id,
                        check_started_at,
                        check_completed_at,
                        check_status,
                        retrieved_status,
                        error_message
                    FROM tracking_checks
                    WHERE shipment_id = ?
                    ORDER BY check_id
                    """,
                    (shipment_id,),
                )

                records = cursor.fetchall()

            finally:
                connection.close()

            print("\nTRACKING CHECK HISTORY\n")

            for record in records:
                print("Check ID:", record[0])
                print("Shipment ID:", record[1])
                print("Started At:", record[2])
                print("Completed At:", record[3])
                print("Check Status:", record[4])
                print("Retrieved Status:", record[5])
                print("Error Message:", record[6])
                print()

            # TEST 4: Verify results
            successful_checks = [
                record for record in records
                if record[4] == "success"
            ]

            failed_checks = [
                record for record in records
                if record[4] == "failed"
            ]

            assert len(records) == 2, (
                f"Expected 2 tracking checks, found {len(records)}"
            )

            assert len(successful_checks) == 1, (
                "Expected exactly 1 successful tracking check"
            )

            assert len(failed_checks) == 1, (
                "Expected exactly 1 failed tracking check"
            )

            assert all(
                record[2] is not None and record[3] is not None
                for record in records
            ), "A tracking check is missing its start or completion time"

            assert successful_checks[0][5] == "In Transit", (
                "Successful check has an unexpected retrieved status"
            )

            assert (
                failed_checks[0][6]
                == "Simulated API connection failure"
            ), "Failed check has an unexpected error message"

            print("TRACKING CHECK REPOSITORY TEST: PASSED")
            print("Successful checks:", len(successful_checks))
            print("Failed checks:", len(failed_checks))
            print("Total recorded checks:", len(records))

    finally:
        if previous_db_path is None:
            os.environ.pop("INDIAPOST_DB_PATH", None)
        else:
            os.environ["INDIAPOST_DB_PATH"] = previous_db_path


if __name__ == "__main__":
    run_test()
