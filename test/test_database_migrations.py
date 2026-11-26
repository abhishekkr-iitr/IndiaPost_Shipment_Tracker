import sqlite3
import unittest

from database.migrations import (
    CONVERSATION_TABLES,
    MigrationError,
    apply_migrations,
    LATEST_SCHEMA_VERSION,
)
from database import schema


class DatabaseMigrationTests(unittest.TestCase):

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.connection.close()

    def table_names(self):
        return {
            row[0]
            for row in self.connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table'
                """
            )
        }

    def test_fresh_database_gets_conversation_schema_and_version(self):
        schema.create_users_table(connection=self.connection)

        result = apply_migrations(self.connection)

        self.assertEqual(result["applied"], [1, 2, 3, 4])
        self.assertEqual(result["current_version"], 4)
        self.assertFalse(result["already_current"])
        self.assertTrue(CONVERSATION_TABLES.issubset(self.table_names()))

        version = self.connection.execute(
            "SELECT version, name FROM schema_migrations"
        ).fetchone()
        self.assertEqual(version, (1, "conversation_persistence"))

        columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info(conversation_tool_calls)"
            )
        }
        self.assertIn("round_id", columns)

    def test_second_run_is_idempotent(self):
        schema.create_users_table(connection=self.connection)

        first = apply_migrations(self.connection)
        second = apply_migrations(self.connection)

        self.assertEqual(first["applied"], [1, 2, 3, 4])
        self.assertEqual(second["applied"], [])
        self.assertEqual(second["current_version"], 4)
        self.assertTrue(second["already_current"])
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0],
            4,
        )

    def test_legacy_tool_calls_table_gets_round_id(self):
        schema.create_users_table(connection=self.connection)
        schema.create_conversations_table(connection=self.connection)
        schema.create_conversation_messages_table(
            connection=self.connection
        )
        schema.create_conversation_requests_table(
            connection=self.connection
        )
        schema.create_conversation_tool_rounds_table(
            connection=self.connection
        )

        self.connection.execute("""
            CREATE TABLE conversation_tool_calls (
                tool_execution_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                request_id TEXT,
                round_number INTEGER NOT NULL CHECK (round_number > 0),
                tool_call_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments TEXT NOT NULL,
                call_message_id INTEGER NOT NULL,
                output_message_id INTEGER,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'pending', 'running', 'completed',
                        'failed', 'interrupted'
                    )
                ),
                error_code TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY (conversation_id)
                    REFERENCES conversations(conversation_id)
                    ON DELETE CASCADE,
                FOREIGN KEY (conversation_id, request_id)
                    REFERENCES conversation_requests(
                        conversation_id, request_id
                    ),
                FOREIGN KEY (conversation_id, call_message_id)
                    REFERENCES conversation_messages(
                        conversation_id, message_id
                    ),
                FOREIGN KEY (conversation_id, output_message_id)
                    REFERENCES conversation_messages(
                        conversation_id, message_id
                    ),
                UNIQUE (conversation_id, tool_call_id)
            )
        """)

        result = apply_migrations(self.connection)

        columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info(conversation_tool_calls)"
            )
        }
        self.assertIn("round_id", columns)
        self.assertEqual(result["applied"], [1, 2, 3, 4])

        index_names = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA index_list(conversation_tool_calls)"
            )
        }
        self.assertIn("idx_conversation_tool_calls_round", index_names)

    def test_partial_unversioned_schema_is_rejected_and_rolled_back(self):
        self.connection.execute("""
            CREATE TABLE conversations (
                conversation_id TEXT PRIMARY KEY
            )
        """)

        with self.assertRaises(MigrationError):
            apply_migrations(self.connection)

        # The migration table was created inside the transaction and rolled
        # back with the rejected migration.
        self.assertNotIn("schema_migrations", self.table_names())
        self.assertEqual(
            self.table_names(),
            {"conversations"},
        )

    def test_supplied_connection_is_not_closed(self):
        schema.create_users_table(connection=self.connection)

        apply_migrations(self.connection)

        # A closed sqlite connection raises ProgrammingError on use.
        self.assertEqual(
            self.connection.execute("SELECT 1").fetchone(),
            (1,),
        )

    def test_active_transaction_is_rejected_without_rollback_of_caller_work(self):
        self.connection.execute(
            "CREATE TABLE caller_work (value TEXT)"
        )
        self.connection.execute(
            "INSERT INTO caller_work (value) VALUES ('keep me')"
        )

        with self.assertRaises(MigrationError):
            apply_migrations(self.connection)

        self.assertTrue(self.connection.in_transaction)
        self.connection.rollback()


    def test_webhook_table_and_indexes_are_created(self):
        schema.create_users_table(connection=self.connection)

        result = apply_migrations(self.connection)

        self.assertEqual(result["current_version"], 4)

        columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info(webhook_events)"
            )
        }

        expected_columns = {
            "event_record_id",
            "provider",
            "provider_event_id",
            "event_type",
            "provider_event_timestamp",
            "provider_tracking_id",
            "tracking_number",
            "carrier",
            "raw_payload",
            "received_at",
            "status",
            "processing_attempts",
            "next_retry_at",
            "claimed_by",
            "claimed_at",
            "lease_expires_at",
            "processed_at",
            "last_error",
        }

        self.assertTrue(expected_columns.issubset(columns))

        indexes = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA index_list(webhook_events)"
            )
        }

        self.assertIn(
            "idx_webhook_events_processing",
            indexes,
        )

        self.assertIn(
            "idx_webhook_events_tracking",
            indexes,
        )

    def test_webhook_event_duplicate_provider_id_is_rejected(self):
        import uuid

        apply_migrations(self.connection)

        event_id = str(uuid.uuid4())

        values = (
            str(uuid.uuid4()),
            "aftership",
            event_id,
            "tracking_update",
            1794825600,
            None,
            "TEST123456",
            "india-post",
            "{}",
            "2026-11-16T00:00:00+00:00",
        )

        self.connection.execute(
            """
            INSERT INTO webhook_events (
                event_record_id,
                provider,
                provider_event_id,
                event_type,
                provider_event_timestamp,
                provider_tracking_id,
                tracking_number,
                carrier,
                raw_payload,
                received_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )

        duplicate_values = (
            str(uuid.uuid4()),
            *values[1:],
        )

        with self.assertRaises(Exception):
            self.connection.execute(
                """
                INSERT INTO webhook_events (
                    event_record_id,
                    provider,
                    provider_event_id,
                    event_type,
                    provider_event_timestamp,
                    provider_tracking_id,
                    tracking_number,
                    carrier,
                    raw_payload,
                    received_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                duplicate_values,
            )

    def test_webhook_status_constraint(self):
        import uuid

        apply_migrations(self.connection)

        with self.assertRaises(Exception):
            self.connection.execute(
                """
                INSERT INTO webhook_events (
                    event_record_id,
                    provider,
                    provider_event_id,
                    event_type,
                    provider_event_timestamp,
                    raw_payload,
                    received_at,
                    status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    "aftership",
                    str(uuid.uuid4()),
                    "tracking_update",
                    1794825600,
                    "{}",
                    "2026-11-16T00:00:00+00:00",
                    "invalid_status",
                ),
            )

    def test_webhook_migration_is_idempotent(self):
        schema.create_users_table(connection=self.connection)

        first = apply_migrations(self.connection)
        second = apply_migrations(self.connection)

        self.assertEqual(first["applied"], [1, 2, 3, 4])
        self.assertEqual(second["applied"], [])
        self.assertTrue(second["already_current"])
        self.assertEqual(second["current_version"], 4)


    def test_notification_channel_preferences_table_and_constraints_are_created(self):
        schema.create_users_table(connection=self.connection)
        result = apply_migrations(self.connection)
        self.assertEqual(result["current_version"], 4)
        columns = {
            row[1] for row in self.connection.execute(
                "PRAGMA table_info(notification_channel_preferences)"
            )
        }
        self.assertTrue({
            "preference_id", "user_id", "channel", "recipient", "enabled",
            "verified_at", "created_at", "updated_at",
        }.issubset(columns))
        indexes = {
            row[1] for row in self.connection.execute(
                "PRAGMA index_list(notification_channel_preferences)"
            )
        }
        self.assertIn("idx_notification_channel_preferences_user_enabled", indexes)


    def _create_monitoring_prerequisite_schema(self):
        """Create the existing shipment tables needed by Migration 6."""
        schema.create_users_table(connection=self.connection)
        schema.create_shipments_table(connection=self.connection)
        schema.create_user_shipments_table(connection=self.connection)
        schema.create_checkpoints_table(connection=self.connection)
        schema.create_tracking_checks_table(connection=self.connection)
        schema.create_notification_preferences_table(connection=self.connection)
        schema.create_notifications_table(connection=self.connection)

    def test_monitoring_migration_creates_table_indexes_and_backfills_jobs(self):
        from datetime import datetime, timezone

        self._create_monitoring_prerequisite_schema()
        now = datetime.now(timezone.utc).isoformat()
        self.connection.executemany(
            "INSERT INTO users (external_user_id, created_at) VALUES (?, ?)",
            [("user-1", now), ("user-2", now), ("user-3", now)],
        )
        self.connection.executemany(
            """INSERT INTO shipments (
                tracking_number, carrier, provider, current_status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            [
                ("TRACK0000001", "india-post", "aftership", "In Transit", now, now),
                ("TRACK0000002", "india-post", "aftership", "Delivered", now, now),
                ("TRACK0000003", "india-post", "aftership", "In Transit", now, now),
                ("TRACK0000004", "india-post", "aftership", "In Transit", now, now),
            ],
        )
        self.connection.executemany(
            """INSERT INTO user_shipments (
                user_id, shipment_id, monitoring_enabled,
                monitoring_interval_hours, next_check_at,
                monitoring_completed_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (1, 1, 1, 6, "2026-11-16T10:00:00+00:00", None, now),
                (2, 1, 1, 12, "2026-11-16T09:00:00+00:00", None, now),
                (3, 2, 1, 6, "2026-11-16T10:00:00+00:00", None, now),
                (1, 3, 0, 6, "2026-11-16T10:00:00+00:00", None, now),
                (1, 4, 1, 6, None, None, now),
                (2, 4, 1, 12, "2099-01-01T00:00:00+00:00", None, now),
            ],
        )
        self.connection.commit()

        result = apply_migrations(self.connection)

        self.assertEqual(result["applied"], list(range(1, LATEST_SCHEMA_VERSION + 1)))
        self.assertEqual(result["current_version"], LATEST_SCHEMA_VERSION)
        columns = {
            row[1] for row in self.connection.execute(
                "PRAGMA table_info(monitoring_jobs)"
            )
        }
        self.assertTrue({
            "monitoring_job_id", "shipment_id", "status", "next_run_at",
            "claimed_by", "claimed_at", "lease_expires_at", "attempt_count",
            "last_attempt_at", "last_success_at", "next_retry_at",
            "last_error", "created_at", "updated_at",
        }.issubset(columns))

        indexes = {
            row[1] for row in self.connection.execute(
                "PRAGMA index_list(monitoring_jobs)"
            )
        }
        self.assertIn("idx_monitoring_jobs_due", indexes)
        self.assertIn("idx_monitoring_jobs_lease", indexes)

        jobs = self.connection.execute(
            """SELECT shipment_id, status, next_run_at
               FROM monitoring_jobs ORDER BY shipment_id"""
        ).fetchall()
        self.assertEqual(len(jobs), 3)
        self.assertEqual(
            jobs[0],
            (1, "scheduled", "2026-11-16T09:00:00+00:00"),
        )
        self.assertEqual(jobs[1][0:2], (2, "completed"))
        self.assertIsNone(jobs[1][2])
        self.assertEqual(jobs[2][0:2], (4, "scheduled"))
        self.assertLessEqual(
            datetime.fromisoformat(jobs[2][2]),
            datetime.now(timezone.utc),
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """INSERT INTO monitoring_jobs (
                    shipment_id, status, created_at, updated_at
                ) VALUES (1, 'scheduled', ?, ?)""",
                (now, now),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """INSERT INTO monitoring_jobs (
                    shipment_id, status, created_at, updated_at
                ) VALUES (9999, 'scheduled', ?, ?)""",
                (now, now),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """INSERT INTO monitoring_jobs (
                    shipment_id, status, created_at, updated_at
                ) VALUES (3, 'invalid_status', ?, ?)""",
                (now, now),
            )

        self.connection.rollback()
        self.assertEqual(
            self.connection.execute("PRAGMA integrity_check").fetchone()[0],
            "ok",
        )
        self.assertEqual(
            self.connection.execute("PRAGMA foreign_key_check").fetchall(),
            [],
        )

    def test_monitoring_backfill_compares_timestamps_by_instant_across_offsets(self):
        from datetime import datetime, timezone

        self._create_monitoring_prerequisite_schema()
        now = datetime.now(timezone.utc).isoformat()
        self.connection.executemany(
            "INSERT INTO users (external_user_id, created_at) VALUES (?, ?)",
            [("user-1", now), ("user-2", now)],
        )
        self.connection.execute(
            """INSERT INTO shipments (
                tracking_number, carrier, provider, current_status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            ("TRACKOFFSET01", "india-post", "aftership", "In Transit", now, now),
        )
        # 10:00 at UTC+05:30 is 04:30 UTC, earlier than 05:00 UTC.
        # Lexicographical MIN() incorrectly chooses the 05:00+00:00 string.
        self.connection.executemany(
            """INSERT INTO user_shipments (
                user_id, shipment_id, monitoring_enabled,
                monitoring_interval_hours, next_check_at,
                monitoring_completed_at, created_at
            ) VALUES (?, 1, 1, 6, ?, NULL, ?)""",
            [
                (1, "2026-11-16T10:00:00+05:30", now),
                (2, "2026-11-16T05:00:00+00:00", now),
            ],
        )
        self.connection.commit()

        apply_migrations(self.connection)
        next_run_at = self.connection.execute(
            "SELECT next_run_at FROM monitoring_jobs WHERE shipment_id = 1"
        ).fetchone()[0]
        self.assertEqual(next_run_at, "2026-11-16T04:30:00+00:00")

    def test_monitoring_migration_is_idempotent_and_preserves_subscription_rows(self):
        from datetime import datetime, timezone

        self._create_monitoring_prerequisite_schema()
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            "INSERT INTO users (external_user_id, created_at) VALUES (?, ?)",
            ("user-1", now),
        )
        self.connection.execute(
            """INSERT INTO shipments (
                tracking_number, carrier, provider, current_status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            ("TRACK0000004", "india-post", "aftership", "In Transit", now, now),
        )
        self.connection.execute(
            """INSERT INTO user_shipments (
                user_id, shipment_id, monitoring_enabled,
                monitoring_interval_hours, next_check_at,
                monitoring_completed_at, created_at
            ) VALUES (1, 1, 1, 8, ?, NULL, ?)""",
            ("2026-11-16T11:00:00+00:00", now),
        )
        self.connection.commit()

        first = apply_migrations(self.connection)
        original_subscription = self.connection.execute(
            """SELECT user_shipment_id, user_id, shipment_id,
                      monitoring_enabled, monitoring_interval_hours,
                      next_check_at, monitoring_completed_at, created_at
               FROM user_shipments"""
        ).fetchone()
        first_job_count = self.connection.execute(
            "SELECT COUNT(*) FROM monitoring_jobs"
        ).fetchone()[0]

        second = apply_migrations(self.connection)
        current_subscription = self.connection.execute(
            """SELECT user_shipment_id, user_id, shipment_id,
                      monitoring_enabled, monitoring_interval_hours,
                      next_check_at, monitoring_completed_at, created_at
               FROM user_shipments"""
        ).fetchone()
        second_job_count = self.connection.execute(
            "SELECT COUNT(*) FROM monitoring_jobs"
        ).fetchone()[0]

        self.assertEqual(first["current_version"], LATEST_SCHEMA_VERSION)
        self.assertEqual(first["applied"], list(range(1, LATEST_SCHEMA_VERSION + 1)))
        self.assertEqual(second["applied"], [])
        self.assertTrue(second["already_current"])
        self.assertEqual(second["current_version"], LATEST_SCHEMA_VERSION)
        self.assertEqual(first_job_count, 1)
        self.assertEqual(second_job_count, 1)
        self.assertEqual(current_subscription, original_subscription)

    def test_monitoring_migration_is_deferred_until_subscription_tables_exist(self):
        schema.create_users_table(connection=self.connection)
        first = apply_migrations(self.connection)
        self.assertEqual(first["current_version"], 4)
        self.assertNotIn("monitoring_jobs", self.table_names())

        schema.create_shipments_table(connection=self.connection)
        schema.create_user_shipments_table(connection=self.connection)
        schema.create_checkpoints_table(connection=self.connection)
        schema.create_tracking_checks_table(connection=self.connection)
        schema.create_notification_preferences_table(connection=self.connection)
        schema.create_notifications_table(connection=self.connection)
        second = apply_migrations(self.connection)

        self.assertEqual(second["applied"], list(range(5, LATEST_SCHEMA_VERSION + 1)))
        self.assertEqual(second["current_version"], LATEST_SCHEMA_VERSION)
        self.assertIn("monitoring_jobs", self.table_names())


    def test_shipment_change_event_migration_creates_table_indexes_and_constraints(self):
        self._create_monitoring_prerequisite_schema()
        result = apply_migrations(self.connection)
        self.assertEqual(result["current_version"], LATEST_SCHEMA_VERSION)
        self.assertIn(7, result["applied"])
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(shipment_change_events)")}
        self.assertTrue({"change_event_id", "shipment_id", "event_type", "schema_version", "source", "idempotency_key", "event_payload", "occurred_at", "recorded_at", "created_at"}.issubset(columns))
        indexes = {row[1] for row in self.connection.execute("PRAGMA index_list(shipment_change_events)")}
        self.assertIn("idx_shipment_change_events_shipment_time", indexes)
        self.assertIn("idx_shipment_change_events_type_time", indexes)
        fk = self.connection.execute("PRAGMA foreign_key_list(shipment_change_events)").fetchall()
        self.assertTrue(any(row[2] == "shipments" and row[3] == "shipment_id" for row in fk))

    def test_shipment_change_event_migration_is_idempotent(self):
        self._create_monitoring_prerequisite_schema()
        first = apply_migrations(self.connection)
        second = apply_migrations(self.connection)
        self.assertEqual(first["current_version"], LATEST_SCHEMA_VERSION)
        self.assertIn(7, first["applied"])
        self.assertEqual(second["applied"], [])
        self.assertTrue(second["already_current"])
        self.assertEqual(second["current_version"], LATEST_SCHEMA_VERSION)



if __name__ == "__main__":
    unittest.main()
