
import os
import tempfile
from pathlib import Path
from datetime import datetime


def run_test():
    previous_db_path = os.environ.get("INDIAPOST_DB_PATH")

    try:
        with tempfile.TemporaryDirectory(
            prefix="IndiaPost_UserShipment_Test_"
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

            # The repository now synchronises Migration 6 monitoring_jobs.
            connection = get_db_connection()
            try:
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS monitoring_jobs (
                        monitoring_job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        shipment_id INTEGER NOT NULL UNIQUE,
                        status TEXT NOT NULL DEFAULT 'scheduled'
                            CHECK (status IN ('scheduled','processing','retryable','completed','disabled')),
                        next_run_at TEXT,
                        claimed_by TEXT,
                        claimed_at TEXT,
                        lease_expires_at TEXT,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        last_attempt_at TEXT,
                        last_success_at TEXT,
                        next_retry_at TEXT,
                        last_error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY (shipment_id) REFERENCES shipments(shipment_id)
                    )
                """)
                connection.commit()
            finally:
                connection.close()

            from database.user_shipment_repository import (
                save_user_shipment,
                get_user_shipment,
                get_user_shipments,
            )

            # Create a fresh synthetic user and shipment.
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
                    ("TEST_USER_SHIPMENT_001", now),
                )

                user_id = cursor.lastrowid

                cursor.execute(
                    """
                    INSERT INTO shipments (
                        tracking_number,
                        carrier,
                        provider,
                        current_status,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "TEST_USER_SHIPMENT_001IN",
                        "india-post",
                        "aftership",
                        "In Transit",
                        now,
                        now,
                    ),
                )

                shipment_id = cursor.lastrowid
                connection.commit()

            finally:
                connection.close()

            print("\nTemporary database:", test_db_path)
            print("Synthetic User ID:", user_id)
            print("Synthetic Shipment ID:", shipment_id)

            # TEST 1: Create a shipment subscription.
            user_shipment_id = save_user_shipment(
                user_id=user_id,
                shipment_id=shipment_id,
                monitoring_interval_hours=2,
                monitoring_enabled=1,
            )

            assert user_shipment_id is not None, (
                "Shipment subscription creation failed"
            )

            print("\nSubscription Created!")
            print("User Shipment ID:", user_shipment_id)

            # TEST 2: Retrieve the initial subscription.
            subscription = get_user_shipment(
                user_id,
                shipment_id,
            )

            assert subscription is not None, (
                "Initial subscription could not be retrieved"
            )

            assert subscription["monitoring_enabled"] == 1
            assert subscription["monitoring_interval_hours"] == 2

            print("\nINITIAL SUBSCRIPTION\n")
            print(
                "Monitoring Enabled:",
                subscription["monitoring_enabled"],
            )
            print(
                "Monitoring Interval:",
                subscription["monitoring_interval_hours"],
            )
            print("Next Check At:", subscription["next_check_at"])

            # TEST 3: Update monitoring interval to 6 hours.
            save_user_shipment(
                user_id=user_id,
                shipment_id=shipment_id,
                monitoring_interval_hours=6,
                monitoring_enabled=1,
            )

            updated_subscription = get_user_shipment(
                user_id,
                shipment_id,
            )

            assert updated_subscription is not None
            assert (
                updated_subscription["monitoring_interval_hours"] == 6
            ), "Monitoring interval was not updated to 6 hours"

            print("\nUPDATED SUBSCRIPTION\n")
            print(
                "Monitoring Interval:",
                updated_subscription["monitoring_interval_hours"],
            )
            print("Next Check At:", updated_subscription["next_check_at"])

            # TEST 4: Disable monitoring.
            save_user_shipment(
                user_id=user_id,
                shipment_id=shipment_id,
                monitoring_interval_hours=6,
                monitoring_enabled=0,
            )

            disabled_subscription = get_user_shipment(
                user_id,
                shipment_id,
            )

            assert disabled_subscription is not None
            assert (
                disabled_subscription["monitoring_enabled"] == 0
            ), "Monitoring was not disabled"

            assert (
                disabled_subscription["next_check_at"] is None
            ), "Next check time was not cleared after disabling monitoring"

            print("\nMONITORING DISABLED\n")
            print(
                "Monitoring Enabled:",
                disabled_subscription["monitoring_enabled"],
            )
            print("Next Check At:", disabled_subscription["next_check_at"])

            # TEST 5: Retrieve all shipments belonging to the user.
            all_shipments = get_user_shipments(user_id)

            print("\nALL USER SHIPMENTS\n")

            for shipment in all_shipments:
                print(
                    "Consignment Number:",
                    shipment["tracking_number"],
                )
                print(
                    "Current Status:",
                    shipment["current_status"],
                )
                print(
                    "Monitoring Interval:",
                    shipment["monitoring_interval_hours"],
                )
                print(
                    "Monitoring Enabled:",
                    shipment["monitoring_enabled"],
                )
                print("------------------------------")

            assert len(all_shipments) == 1, (
                f"Expected 1 shipment, found {len(all_shipments)}"
            )

            # TEST 6: Final verification.
            assert (
                all_shipments[0]["tracking_number"]
                == "TEST_USER_SHIPMENT_001IN"
            ), "Unexpected shipment returned for the user"

            assert (
                all_shipments[0]["monitoring_enabled"] == 0
            ), "Retrieved shipment does not reflect disabled monitoring"

            assert (
                all_shipments[0]["monitoring_interval_hours"] == 6
            ), "Retrieved shipment does not reflect the updated interval"

            print("\nUSER-SHIPMENT REPOSITORY TEST: PASSED")

    finally:
        if previous_db_path is None:
            os.environ.pop("INDIAPOST_DB_PATH", None)
        else:
            os.environ["INDIAPOST_DB_PATH"] = previous_db_path


if __name__ == "__main__":
    run_test()
