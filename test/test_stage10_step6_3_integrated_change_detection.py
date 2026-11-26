"""Consolidated integration coverage for Stage 10 Step 6.3."""
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from database.db_connection import get_db_connection
from database import schema
from database.migrations import apply_migrations


class IntegratedChangeDetectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stage10_step6_3_")
        self.db_path = str(Path(self.temp.name) / "integration.sqlite3")
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
        self.migration_result = apply_migrations()
        self.assertGreaterEqual(self.migration_result["current_version"], 7)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def connection(self):
        return get_db_connection()

    def add_shipment(self, status="Booked"):
        connection = self.connection()
        try:
            connection.execute(
                """INSERT INTO shipments
                   (tracking_number, carrier, provider, current_status, created_at, updated_at)
                   VALUES (?, ?, 'aftership', ?, '2026-11-01', '2026-11-01')""",
                ("AB123456789IN", "india-post", status),
            )
            connection.commit()
            return connection.execute(
                "SELECT shipment_id FROM shipments WHERE tracking_number = 'AB123456789IN'"
            ).fetchone()[0]
        finally:
            connection.close()

    def test_monitoring_persists_status_and_checkpoint_events_atomically(self):
        shipment_id = self.add_shipment()
        import monitoring.tracking_executor as executor
        tracking_data = {
            "tracking_number": "AB123456789IN",
            "carrier": "india-post",
            "current_status": "InTransit",
            "checkpoints": [{
                "date": "2026-11-16T10:00:00+00:00",
                "location": "New Delhi",
                "status": "InTransit",
                "message": "Shipment dispatched.",
            }],
        }
        with patch.object(executor, "find_tracking_id", return_value="mock-provider-id"), \
             patch.object(executor, "track_shipment", return_value=tracking_data), \
             patch.object(executor, "start_tracking_check", return_value=1001), \
             patch.object(executor, "complete_tracking_check"):
            result = executor.execute_shipment_tracking({
                "shipment_id": shipment_id,
                "tracking_number": "AB123456789IN",
                "carrier": "india-post",
                "current_status": "Booked",
            })
        self.assertTrue(result["success"])
        self.assertEqual(result["inserted_checkpoints"], 1)
        self.assertTrue(result["change_detection"]["status_changed"])
        self.assertEqual(len(result["change_detection"]["checkpoint_events"]), 1)
        connection = self.connection()
        try:
            rows = connection.execute(
                "SELECT event_type, source FROM shipment_change_events ORDER BY event_type"
            ).fetchall()
            self.assertEqual(rows, [
                ("shipment.checkpoint_added", "monitoring"),
                ("shipment.status_changed", "monitoring"),
            ])
        finally:
            connection.close()

    def test_monitoring_repeat_does_not_duplicate_events(self):
        shipment_id = self.add_shipment()
        import monitoring.tracking_executor as executor
        tracking_data = {
            "tracking_number": "AB123456789IN", "carrier": "india-post",
            "current_status": "InTransit",
            "checkpoints": [{
                "date": "2026-11-16T10:00:00+00:00", "location": "New Delhi",
                "status": "InTransit", "message": "Shipment dispatched.",
            }],
        }
        with patch.object(executor, "find_tracking_id", return_value="mock-provider-id"), \
             patch.object(executor, "track_shipment", return_value=tracking_data), \
             patch.object(executor, "start_tracking_check", side_effect=[1001, 1002]), \
             patch.object(executor, "complete_tracking_check"):
            first = executor.execute_shipment_tracking({
                "shipment_id": shipment_id, "tracking_number": "AB123456789IN",
                "carrier": "india-post", "current_status": "Booked",
            })
            second = executor.execute_shipment_tracking({
                "shipment_id": shipment_id, "tracking_number": "AB123456789IN",
                "carrier": "india-post", "current_status": "InTransit",
            })
        self.assertEqual(first["inserted_checkpoints"], 1)
        self.assertEqual(second["inserted_checkpoints"], 0)
        connection = self.connection()
        try:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM shipment_change_events"
            ).fetchone()[0], 2)
        finally:
            connection.close()

    def test_stale_monitoring_response_preserves_newer_webhook_status(self):
        shipment_id = self.add_shipment(status="Booked")
        import monitoring.tracking_executor as executor

        def delayed_provider_response(*args, **kwargs):
            # Simulate a webhook committing a newer status while the monitoring
            # provider request is in flight, before monitoring persistence starts.
            connection = self.connection()
            try:
                connection.execute(
                    "UPDATE shipments SET current_status = ? WHERE shipment_id = ?",
                    ("Delivered", shipment_id),
                )
                connection.commit()
            finally:
                connection.close()
            return {
                "tracking_number": "AB123456789IN", "carrier": "india-post",
                "current_status": "InTransit", "checkpoints": [],
            }

        with patch.object(executor, "find_tracking_id", return_value="mock-provider-id"), \
             patch.object(executor, "track_shipment", side_effect=delayed_provider_response), \
             patch.object(executor, "start_tracking_check", return_value=2001), \
             patch.object(executor, "complete_tracking_check"):
            result = executor.execute_shipment_tracking({
                "shipment_id": shipment_id, "tracking_number": "AB123456789IN",
                "carrier": "india-post", "current_status": "Booked",
            })

        self.assertTrue(result["success"])
        self.assertEqual(result["tracking_data"]["current_status"], "Delivered")
        connection = self.connection()
        try:
            self.assertEqual(connection.execute(
                "SELECT current_status FROM shipments WHERE shipment_id = ?", (shipment_id,)
            ).fetchone()[0], "Delivered")
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM shipment_change_events WHERE event_type='shipment.status_changed'"
            ).fetchone()[0], 0)
        finally:
            connection.close()

    def test_webhook_records_v2_status_and_checkpoint_events(self):
        payload = {
            "ts": 1794825600,
            "event": "tracking_update",
            "event_id": str(uuid.uuid4()),
            "msg": {
                "id": "mock-provider-id", "tracking_number": "CD123456789IN",
                "slug": "india-post", "tag": "InTransit",
                "subtag_message": "Moving through network.",
                "origin_raw_location": "New Delhi", "destination_raw_location": "Mumbai",
                "shipment_pickup_date": "2026-11-15", "shipment_delivery_date": None,
                "checkpoints": [{
                    "checkpoint_time": "2026-11-15T10:00:00Z", "location": "New Delhi",
                    "tag": "InTransit", "message": "Shipment dispatched.",
                }],
            },
        }
        from database.webhook_event_repository import save_webhook_event, claim_next_webhook_event
        from webhooks.webhook_event_processor import process_claimed_webhook_event
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        saved = save_webhook_event(payload, raw)
        claimed = claim_next_webhook_event("step63-integration-worker")
        self.assertTrue(saved["inserted"])
        self.assertIsNotNone(claimed)
        result = process_claimed_webhook_event(
            saved["event_record_id"], "step63-integration-worker"
        )
        self.assertTrue(result["processed"])
        self.assertTrue(result["change_detection"]["status_changed"])
        self.assertEqual(len(result["change_detection"]["checkpoint_events"]), 1)
        connection = self.connection()
        try:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM shipment_change_events WHERE source='webhook'"
            ).fetchone()[0], 2)
        finally:
            connection.close()

    def test_v2_status_event_uses_existing_notification_pipeline(self):
        from database.notification_channel_preference_repository import save_channel_preference
        from notifications.generation import generate_notifications_in_transaction
        self.add_shipment(status="In Transit")
        connection = self.connection()
        try:
            connection.execute(
                "INSERT INTO users(external_user_id, created_at) VALUES ('registered-user', '2026-01-01')"
            )
            connection.execute(
                "INSERT INTO user_shipments(user_id, shipment_id, created_at) VALUES (1, 1, '2026-01-01')"
            )
            connection.commit()
        finally:
            connection.close()
        save_channel_preference(1, "telegram", "123456789", True,
                                "2026-11-16T12:00:00+00:00")
        payload = {
            "event_id": str(uuid.uuid4()),
            "event_type": "shipment.status_changed",
            "schema_version": 2,
            "source": "monitoring",
            "occurred_at": "2026-11-16T10:00:00+00:00",
            "recorded_at": "2026-11-16T12:00:00+00:00",
            "shipment": {"shipment_id": 1, "tracking_number": "AB123456789IN", "carrier": "india-post"},
            "change": {"previous_status": "Booked", "new_status": "In Transit"},
        }
        connection = self.connection()
        try:
            result = generate_notifications_in_transaction(connection, payload)
            self.assertEqual(result["status"], "generated")
            self.assertEqual(result["queued_or_already_queued"], 1)
            row = connection.execute(
                "SELECT previous_status, new_status, delivery_status FROM notifications"
            ).fetchone()
            self.assertEqual(row, ("Booked", "In Transit", "pending"))
        finally:
            connection.close()

    def test_v2_checkpoint_event_does_not_generate_notification(self):
        from notifications.generation import generate_notifications_in_transaction
        payload = {
            "event_id": str(uuid.uuid4()),
            "event_type": "shipment.checkpoint_added",
            "schema_version": 2,
            "source": "monitoring",
            "occurred_at": "2026-11-16T10:00:00+00:00",
            "recorded_at": "2026-11-16T10:01:00+00:00",
            "shipment": {"shipment_id": 1, "tracking_number": "AB123456789IN", "carrier": "india-post"},
            "change": {"checkpoint_id": 5, "checkpoint_event_at": "2026-11-16T10:00:00+00:00"},
        }
        self.add_shipment()
        connection = self.connection()
        try:
            result = generate_notifications_in_transaction(connection, payload)
            self.assertEqual(result["status"], "event_not_notifiable")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 0)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
