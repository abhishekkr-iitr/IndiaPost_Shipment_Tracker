"""Step 7.5 integration tests for policy-driven checkpoint notification generation."""
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from database.db_connection import get_db_connection
from database import schema
from database.migrations import apply_migrations
from database.notification_channel_preference_repository import save_channel_preference
from notifications.generation import generate_notifications_for_change_detection_in_transaction


class CheckpointNotificationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stage10_step7_5_")
        self.db_path = str(Path(self.temp.name) / "step7_5.sqlite3")
        self.env = patch.dict(os.environ, {"INDIAPOST_DB_PATH": self.db_path})
        self.env.start()
        connection = get_db_connection()
        try:
            for creator in (
                schema.create_users_table,
                schema.create_shipments_table,
                schema.create_user_shipments_table,
                schema.create_checkpoints_table,
                schema.create_tracking_checks_table,
                schema.create_notification_preferences_table,
                schema.create_notifications_table,
            ):
                creator(connection)
            connection.commit()
        finally:
            connection.close()
        self.assertGreaterEqual(apply_migrations()["current_version"], 7)
        connection = get_db_connection()
        try:
            connection.execute(
                "INSERT INTO shipments (tracking_number, carrier, provider, current_status, created_at, updated_at) "
                "VALUES ('AB123456789IN', 'india-post', 'aftership', 'InTransit', '2026-11-01', '2026-11-01')"
            )
            connection.execute(
                "INSERT INTO users (external_user_id, created_at) VALUES ('step7-5-user', '2026-11-01')"
            )
            connection.execute(
                "INSERT INTO user_shipments (user_id, shipment_id, monitoring_enabled, created_at) "
                "VALUES (1, 1, 1, '2026-11-01')"
            )
            connection.execute(
                "INSERT INTO checkpoints (checkpoint_id, shipment_id, event_date, location, status, message, event_fingerprint, created_at) "
                "VALUES (101, 1, '2026-11-16T10:00:00+00:00', 'Bengaluru', 'Delivered', 'Shipment delivered.', 'step7-5-checkpoint', '2026-11-16T10:00:00+00:00')"
            )
            connection.commit()
        finally:
            connection.close()
        save_channel_preference(
            1, "telegram", "123456789", True, "2026-11-16T12:00:00+00:00"
        )

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def _event(self, *, event_type, change, source="monitoring"):
        return {
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "schema_version": 2,
            "source": source,
            "occurred_at": "2026-11-16T10:00:00+00:00",
            "recorded_at": "2026-11-16T10:01:00+00:00",
            "shipment": {
                "shipment_id": 1,
                "tracking_number": "AB123456789IN",
                "carrier": "india-post",
            },
            "change": change,
        }

    def test_meaningful_checkpoint_is_enqueued_through_shared_transaction_helper(self):
        checkpoint = self._event(
            event_type="shipment.checkpoint_added",
            change={
                "checkpoint_id": 101,
                "checkpoint_event_at": "2026-11-16T10:00:00+00:00",
                "status": "Delivered",
                "location": "Bengaluru",
                "message": "Shipment delivered.",
            },
        )
        connection = get_db_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = generate_notifications_for_change_detection_in_transaction(
                connection,
                {"status_event": None, "checkpoint_events": [{"event_payload": checkpoint}]},
            )
            self.assertEqual(result["status"], "generated")
            self.assertEqual(result["checkpoint_events_processed"], 1)
            self.assertEqual(result["checkpoint_notifications_queued_or_existing"], 1)
            row = connection.execute(
                "SELECT checkpoint_id, previous_status, new_status, delivery_status FROM notifications"
            ).fetchone()
            self.assertEqual(row, (101, None, "delivered", "pending"))
            self.assertTrue(connection.in_transaction)
            connection.rollback()
        finally:
            connection.close()

    def test_routine_checkpoint_is_suppressed_without_removing_event(self):
        checkpoint = self._event(
            event_type="shipment.checkpoint_added",
            change={
                "checkpoint_id": 101,
                "checkpoint_event_at": "2026-11-16T10:00:00+00:00",
                "status": "InTransit",
                "location": "Sorting Office",
                "message": "Shipment dispatched.",
            },
        )
        connection = get_db_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO shipment_change_events "
                "(change_event_id, shipment_id, event_type, schema_version, source, idempotency_key, event_payload, occurred_at, recorded_at, created_at) "
                "VALUES (?, 1, 'shipment.checkpoint_added', 2, 'monitoring', ?, '{}', '2026-11-16T10:00:00+00:00', '2026-11-16T10:01:00+00:00', '2026-11-16T10:01:00+00:00')",
                (checkpoint["event_id"], "test-ledger-" + checkpoint["event_id"]),
            )
            result = generate_notifications_for_change_detection_in_transaction(
                connection,
                {"status_event": None, "checkpoint_events": [{"event_payload": checkpoint}]},
            )
            self.assertEqual(result["status"], "event_not_notifiable")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM shipment_change_events").fetchone()[0], 1)
            connection.rollback()
        finally:
            connection.close()

    def test_status_result_shape_is_preserved_when_checkpoint_events_are_also_processed(self):
        status = self._event(
            event_type="shipment.status_changed",
            change={"previous_status": "InTransit", "new_status": "Delivered"},
        )
        checkpoint = self._event(
            event_type="shipment.checkpoint_added",
            change={"checkpoint_id": 101, "status": "Delivered", "message": "Delivered."},
        )
        connection = get_db_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = generate_notifications_for_change_detection_in_transaction(
                connection,
                {
                    "status_event": {"event_payload": status},
                    "checkpoint_events": [{"event_payload": checkpoint}],
                },
            )
            self.assertEqual(result["status"], "generated")
            self.assertIn("queued_or_already_queued", result)
            self.assertEqual(result["checkpoint_events_processed"], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 2)
            connection.rollback()
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
