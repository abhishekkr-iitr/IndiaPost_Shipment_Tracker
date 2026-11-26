
import os
import tempfile
from pathlib import Path


def run_test():
    previous_db_path = os.environ.get("INDIAPOST_DB_PATH")

    try:
        with tempfile.TemporaryDirectory(
            prefix="IndiaPost_DatabaseSchema_Test_"
        ) as temp_dir:

            test_db_path = Path(temp_dir) / "test_shipments.db"
            os.environ["INDIAPOST_DB_PATH"] = str(test_db_path)

            # Import only after setting the temporary database path.
            from database.schema import (
                create_users_table,
                create_shipments_table,
                create_user_shipments_table,
                create_checkpoints_table,
                create_tracking_checks_table,
                create_notification_preferences_table,
                create_notifications_table,
            )

            from database.db_connection import get_db_connection
            from database.migrations import LATEST_SCHEMA_VERSION, apply_migrations

            # Create tables in dependency order.
            create_users_table()
            create_shipments_table()
            create_user_shipments_table()
            create_checkpoints_table()
            create_tracking_checks_table()
            create_notification_preferences_table()
            create_notifications_table()

            # Apply explicit migrations through the latest application schema.
            migration_result = apply_migrations()
            assert migration_result["current_version"] == LATEST_SCHEMA_VERSION, migration_result

            print("\nTemporary database:", test_db_path)

            connection = get_db_connection()

            try:
                cursor = connection.cursor()

                # TEST 1: Verify that all required tables exist.
                expected_tables = {
                    "users",
                    "shipments",
                    "user_shipments",
                    "checkpoints",
                    "tracking_checks",
                    "notification_preferences",
                    "notifications",
                    "monitoring_jobs",
                    "shipment_change_events",
                }

                cursor.execute("""
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table';
                """)

                existing_tables = {
                    row[0] for row in cursor.fetchall()
                }

                missing_tables = expected_tables - existing_tables

                assert not missing_tables, (
                    f"Missing database tables: {sorted(missing_tables)}"
                )

                print("\nALL REQUIRED TABLES VERIFIED")

                for table in sorted(expected_tables):
                    print("Table found:", table)

                # TEST 2: Inspect table structures and foreign keys.
                tables_to_inspect = [
                    "shipments",
                    "user_shipments",
                    "checkpoints",
                    "tracking_checks",
                    "notification_preferences",
                    "notifications",
                    "monitoring_jobs",
                    "shipment_change_events",
                ]

                table_columns = {}
                table_foreign_keys = {}

                for table in tables_to_inspect:

                    cursor.execute(
                        f"PRAGMA table_info({table});"
                    )

                    columns = cursor.fetchall()

                    assert columns, (
                        f"No columns found in table: {table}"
                    )

                    table_columns[table] = {
                        column[1] for column in columns
                    }

                    print(f"\n{table.upper()} TABLE STRUCTURE\n")

                    for column in columns:
                        print(
                            "Column:", column[1],
                            "| Data Type:", column[2]
                        )

                    cursor.execute(
                        f"PRAGMA foreign_key_list({table});"
                    )

                    foreign_keys = cursor.fetchall()

                    table_foreign_keys[table] = {
                        (fk[2], fk[3], fk[4])
                        for fk in foreign_keys
                    }

                    print(f"\n{table.upper()} FOREIGN KEYS\n")

                    for foreign_key in foreign_keys:
                        print(
                            "Referenced Table:", foreign_key[2],
                            "| Local Column:", foreign_key[3],
                            "| Referenced Column:", foreign_key[4]
                        )

                # TEST 3: Verify required foreign-key relationships.
                expected_foreign_keys = {
                    "user_shipments": {
                        ("users", "user_id", "user_id"),
                        ("shipments", "shipment_id", "shipment_id"),
                    },
                    "checkpoints": {
                        ("shipments", "shipment_id", "shipment_id"),
                    },
                    "tracking_checks": {
                        ("shipments", "shipment_id", "shipment_id"),
                    },
                    "notification_preferences": {
                        ("users", "user_id", "user_id"),
                    },
                    "notifications": {
                        (
                            "user_shipments",
                            "user_shipment_id",
                            "user_shipment_id",
                        ),
                        (
                            "checkpoints",
                            "checkpoint_id",
                            "checkpoint_id",
                        ),
                    },
                    "monitoring_jobs": {
                        ("shipments", "shipment_id", "shipment_id"),
                    },
                    "shipment_change_events": {
                        ("shipments", "shipment_id", "shipment_id"),
                    },
                }

                for table, expected in expected_foreign_keys.items():

                    actual = table_foreign_keys[table]

                    missing = expected - actual

                    assert not missing, (
                        f"Missing foreign keys in {table}: {missing}"
                    )

                assert "idx_monitoring_jobs_due" in {
                    row[1] for row in cursor.execute(
                        "PRAGMA index_list(monitoring_jobs);"
                    ).fetchall()
                }
                assert "idx_monitoring_jobs_lease" in {
                    row[1] for row in cursor.execute(
                        "PRAGMA index_list(monitoring_jobs);"
                    ).fetchall()
                }
                assert "idx_shipment_change_events_shipment_time" in {
                    row[1] for row in cursor.execute(
                        "PRAGMA index_list(shipment_change_events);"
                    ).fetchall()
                }
                assert "idx_shipment_change_events_type_time" in {
                    row[1] for row in cursor.execute(
                        "PRAGMA index_list(shipment_change_events);"
                    ).fetchall()
                }

                print("\nFOREIGN KEY RELATIONSHIPS VERIFIED")

                # TEST 4: Check database integrity.
                cursor.execute("PRAGMA integrity_check;")
                integrity_result = cursor.fetchone()[0]

                assert integrity_result == "ok", (
                    f"Database integrity check failed: {integrity_result}"
                )

                print("Database integrity:", integrity_result)

                # TEST 5: Check foreign-key integrity.
                cursor.execute("PRAGMA foreign_key_check;")
                foreign_key_errors = cursor.fetchall()

                assert not foreign_key_errors, (
                    f"Foreign-key integrity errors: {foreign_key_errors}"
                )

                print("Foreign-key integrity: OK")

                print("\nDATABASE SCHEMA TEST: PASSED")

            finally:
                connection.close()

    finally:
        if previous_db_path is None:
            os.environ.pop("INDIAPOST_DB_PATH", None)
        else:
            os.environ["INDIAPOST_DB_PATH"] = previous_db_path


if __name__ == "__main__":
    run_test()
