import json
import os
import tempfile
import unittest
import uuid

from pathlib import Path
from unittest.mock import patch


class TestWebhookEventProcessor(unittest.TestCase):

    WORKER_ID = "processor-test-worker"

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()

        self.database_path = (
            Path(self.temp_directory.name) / "processor_test.db"
        )

        self.environment_patch = patch.dict(
            os.environ,
            {
                "INDIAPOST_DB_PATH": str(self.database_path)
            },
        )

        self.environment_patch.start()

        from database.db_connection import get_db_connection
        from database.migrations import apply_migrations
        from database import schema

        connection = get_db_connection()

        try:
            self.migration_result = apply_migrations(connection)

            schema.create_checkpoints_table(
                connection=connection
            )

            connection.commit()

        finally:
            connection.close()

        from database.webhook_event_repository import (
            save_webhook_event,
            claim_next_webhook_event,
            get_webhook_event,
        )

        from webhooks.webhook_event_processor import (
            process_claimed_webhook_event,
            normalize_aftership_webhook_event,
        )

        self.save_webhook_event = save_webhook_event
        self.claim_next_webhook_event = claim_next_webhook_event
        self.get_webhook_event = get_webhook_event

        self.process_claimed_webhook_event = (
            process_claimed_webhook_event
        )

        self.normalize_aftership_webhook_event = (
            normalize_aftership_webhook_event
        )

    def tearDown(self):
        self.environment_patch.stop()
        self.temp_directory.cleanup()

    # ---------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------

    def get_connection(self):
        from database.db_connection import get_db_connection

        return get_db_connection()

    def make_payload(
        self,
        event_type="tracking_update",
        event_timestamp=1794825600,
        tracking_number="TEST123456",
        carrier="india-post",
        status="InTransit",
        checkpoints=None,
    ):
        return {
            "ts": event_timestamp,
            "event": event_type,
            "event_id": str(uuid.uuid4()),
            "msg": {
                "id": "synthetic-tracking-id",
                "tracking_number": tracking_number,
                "slug": carrier,
                "tag": status,
                "subtag_message": "Synthetic test status",
                "origin_raw_location": "New Delhi",
                "destination_raw_location": "Mumbai",
                "shipment_pickup_date": "2026-11-15",
                "shipment_delivery_date": None,
                "checkpoints": checkpoints or [],
            },
        }

    def create_and_claim_event(self, payload):
        raw_payload = json.dumps(
            payload,
            separators=(",", ":"),
        ).encode("utf-8")

        saved = self.save_webhook_event(
            payload,
            raw_payload,
        )

        self.assertTrue(saved["inserted"])

        claimed = self.claim_next_webhook_event(
            self.WORKER_ID
        )

        self.assertTrue(claimed)

        return saved["event_record_id"]

    def process_payload(self, payload):
        event_record_id = self.create_and_claim_event(payload)

        result = self.process_claimed_webhook_event(
            event_record_id,
            self.WORKER_ID,
        )

        return event_record_id, result

    def get_shipment(self, tracking_number, carrier="india-post"):
        connection = self.get_connection()

        try:
            cursor = connection.execute(
                """
                SELECT *
                FROM shipments
                WHERE tracking_number = ?
                  AND carrier = ?
                """,
                (tracking_number, carrier),
            )

            row = cursor.fetchone()

            if row is None:
                return None

            columns = [
                description[0]
                for description in cursor.description
            ]

            return dict(zip(columns, row))

        finally:
            connection.close()

    def get_checkpoint_count(self, shipment_id):
        connection = self.get_connection()

        try:
            return connection.execute(
                """
                SELECT COUNT(*)
                FROM checkpoints
                WHERE shipment_id = ?
                """,
                (shipment_id,),
            ).fetchone()[0]

        finally:
            connection.close()

    def get_table_count(self, table_name):
        allowed_tables = {
            "shipments",
            "checkpoints",
            "users",
            "user_shipments",
            "notification_preferences",
            "notifications",
        }

        self.assertIn(table_name, allowed_tables)

        connection = self.get_connection()

        try:
            table_exists = connection.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table'
                  AND name = ?
                """,
                (table_name,),
            ).fetchone()

            if table_exists is None:
                return 0

            return connection.execute(
                f"SELECT COUNT(*) FROM {table_name}"
            ).fetchone()[0]

        finally:
            connection.close()

    # ---------------------------------------------------------
    # Migration and normalisation
    # ---------------------------------------------------------

    def test_migration_version_three_is_available(self):
        # Migration 4 adds channel preferences. Migration 5 deliberately
        # defers in this minimal webhook fixture because notifications is
        # not created by the webhook-only schema setup.
        self.assertEqual(
            self.migration_result["current_version"],
            4,
        )

        connection = self.get_connection()

        try:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(shipments)"
                )
            }

            self.assertIn(
                "last_provider_event_timestamp",
                columns,
            )

        finally:
            connection.close()

    def test_normalizer_maps_tracking_update_fields(self):
        payload = self.make_payload(
            checkpoints=[
                {
                    "checkpoint_time": "2026-11-15T10:00:00Z",
                    "location": "New Delhi",
                    "tag": "PickedUp",
                    "message": "Shipment picked up",
                }
            ]
        )

        normalized = self.normalize_aftership_webhook_event(
            payload
        )

        self.assertEqual(
            normalized["event_type"],
            "tracking_update",
        )

        self.assertEqual(
            normalized["tracking_number"],
            "TEST123456",
        )

        self.assertEqual(
            normalized["carrier"],
            "india-post",
        )

        self.assertEqual(
            normalized["current_status"],
            "InTransit",
        )

        self.assertEqual(
            normalized["origin"],
            "New Delhi",
        )

        self.assertEqual(
            len(normalized["checkpoints"]),
            1,
        )

        self.assertEqual(
            normalized["checkpoints"][0]["status"],
            "PickedUp",
        )

    def test_normalizer_rejects_invalid_message(self):
        with self.assertRaises(ValueError):
            self.normalize_aftership_webhook_event({
                "event": "tracking_update",
                "ts": 1794825600,
                "msg": None,
            })

    # ---------------------------------------------------------
    # Shipment creation
    # ---------------------------------------------------------

    def test_tracking_update_creates_unknown_shipment(self):
        payload = self.make_payload()

        event_record_id, result = self.process_payload(payload)

        self.assertTrue(result["processed"])
        self.assertTrue(result["shipment_created"])
        self.assertTrue(result["status_updated"])

        self.assertIsNotNone(result["shipment_id"])

        shipment = self.get_shipment("TEST123456")

        self.assertIsNotNone(shipment)

        self.assertEqual(
            shipment["current_status"],
            "InTransit",
        )

        self.assertEqual(
            shipment["status_message"],
            "Synthetic test status",
        )

        self.assertEqual(
            shipment["last_provider_event_timestamp"],
            float(payload["ts"]),
        )

        event = self.get_webhook_event(event_record_id)

        self.assertEqual(event["status"], "processed")

    def test_edd_revise_creates_shipment_without_status(self):
        payload = self.make_payload(
            event_type="edd_revise",
            status="Delivered",
        )

        _, result = self.process_payload(payload)

        self.assertTrue(result["processed"])
        self.assertTrue(result["shipment_created"])
        self.assertFalse(result["status_updated"])

        shipment = self.get_shipment("TEST123456")

        self.assertIsNotNone(shipment)

        self.assertIsNone(shipment["current_status"])
        self.assertIsNone(shipment["status_message"])

        self.assertIsNone(
            shipment["last_provider_event_timestamp"]
        )

    def test_tracking_pending_time_creates_shipment_without_status(self):
        payload = self.make_payload(
            event_type="tracking_pending_time",
            status="InTransit",
        )

        _, result = self.process_payload(payload)

        self.assertTrue(result["processed"])
        self.assertTrue(result["shipment_created"])
        self.assertFalse(result["status_updated"])

        shipment = self.get_shipment("TEST123456")

        self.assertIsNotNone(shipment)
        self.assertIsNone(shipment["current_status"])
        self.assertIsNone(
            shipment["last_provider_event_timestamp"]
        )

    # ---------------------------------------------------------
    # Missing shipment identifiers
    # ---------------------------------------------------------

    def test_event_without_identifiers_is_processed_without_shipment(self):
        payload = self.make_payload(
            tracking_number=None,
            carrier=None,
        )

        event_record_id, result = self.process_payload(payload)

        self.assertTrue(result["processed"])
        self.assertIsNone(result["shipment_id"])
        self.assertFalse(result["shipment_created"])

        self.assertEqual(
            self.get_table_count("shipments"),
            0,
        )

        event = self.get_webhook_event(event_record_id)

        self.assertEqual(event["status"], "processed")

    def test_event_with_only_tracking_number_does_not_create_shipment(self):
        payload = self.make_payload(
            tracking_number="TEST123456",
            carrier=None,
        )

        event_record_id, result = self.process_payload(payload)

        self.assertTrue(result["processed"])
        self.assertIsNone(result["shipment_id"])

        self.assertEqual(
            self.get_table_count("shipments"),
            0,
        )

        event = self.get_webhook_event(event_record_id)

        self.assertEqual(event["status"], "processed")

    def test_event_with_only_carrier_does_not_create_shipment(self):
        payload = self.make_payload(
            tracking_number=None,
            carrier="india-post",
        )

        event_record_id, result = self.process_payload(payload)

        self.assertTrue(result["processed"])
        self.assertIsNone(result["shipment_id"])

        self.assertEqual(
            self.get_table_count("shipments"),
            0,
        )

        event = self.get_webhook_event(event_record_id)

        self.assertEqual(event["status"], "processed")

    # ---------------------------------------------------------
    # Existing shipment and event ordering
    # ---------------------------------------------------------

    def test_newer_tracking_update_updates_existing_shipment(self):
        first = self.make_payload(
            event_timestamp=1794825600,
            status="InTransit",
        )

        _, first_result = self.process_payload(first)

        shipment_id = first_result["shipment_id"]

        second = self.make_payload(
            event_timestamp=1794829200,
            status="OutForDelivery",
        )

        _, second_result = self.process_payload(second)

        self.assertFalse(second_result["shipment_created"])
        self.assertTrue(second_result["status_updated"])

        shipment = self.get_shipment("TEST123456")

        self.assertEqual(
            shipment["shipment_id"],
            shipment_id,
        )

        self.assertEqual(
            shipment["current_status"],
            "OutForDelivery",
        )

        self.assertEqual(
            shipment["last_provider_event_timestamp"],
            float(second["ts"]),
        )

    def test_older_event_does_not_regress_shipment_status(self):
        first = self.make_payload(
            event_timestamp=1794829200,
            status="OutForDelivery",
        )

        _, first_result = self.process_payload(first)

        shipment_id = first_result["shipment_id"]

        older = self.make_payload(
            event_timestamp=1794825600,
            status="InTransit",
            checkpoints=[
                {
                    "checkpoint_time": "2026-11-14T08:00:00Z",
                    "location": "Pune",
                    "tag": "InTransit",
                    "message": "Historical checkpoint",
                }
            ],
        )

        _, older_result = self.process_payload(older)

        self.assertFalse(older_result["shipment_created"])
        self.assertFalse(older_result["status_updated"])

        shipment = self.get_shipment("TEST123456")

        self.assertEqual(
            shipment["current_status"],
            "OutForDelivery",
        )

        self.assertEqual(
            shipment["last_provider_event_timestamp"],
            float(first["ts"]),
        )

        self.assertEqual(
            self.get_checkpoint_count(shipment_id),
            1,
        )

    def test_special_event_does_not_change_existing_status(self):
        first = self.make_payload(
            event_timestamp=1794825600,
            status="InTransit",
        )

        _, first_result = self.process_payload(first)

        shipment_id = first_result["shipment_id"]

        second = self.make_payload(
            event_type="edd_revise",
            event_timestamp=1794829200,
            status="Delivered",
        )

        _, second_result = self.process_payload(second)

        self.assertFalse(second_result["shipment_created"])
        self.assertFalse(second_result["status_updated"])

        shipment = self.get_shipment("TEST123456")

        self.assertEqual(
            shipment["shipment_id"],
            shipment_id,
        )

        self.assertEqual(
            shipment["current_status"],
            "InTransit",
        )

        self.assertEqual(
            shipment["last_provider_event_timestamp"],
            float(first["ts"]),
        )

    # ---------------------------------------------------------
    # Checkpoint idempotency
    # ---------------------------------------------------------

    def test_duplicate_checkpoints_are_not_inserted_twice(self):
        checkpoint = {
            "checkpoint_time": "2026-11-15T10:00:00Z",
            "location": "New Delhi",
            "tag": "PickedUp",
            "message": "Shipment picked up",
        }

        first = self.make_payload(
            checkpoints=[checkpoint],
        )

        _, first_result = self.process_payload(first)

        shipment_id = first_result["shipment_id"]

        second = self.make_payload(
            event_timestamp=1794829200,
            checkpoints=[checkpoint],
        )

        _, second_result = self.process_payload(second)

        self.assertEqual(
            self.get_checkpoint_count(shipment_id),
            1,
        )

        self.assertEqual(
            second_result["checkpoints_inserted"],
            0,
        )

    # ---------------------------------------------------------
    # Transaction atomicity
    # ---------------------------------------------------------

    def test_checkpoint_failure_rolls_back_shipment_and_event_completion(self):
        payload = self.make_payload(
            checkpoints=[
                {
                    "checkpoint_time": "2026-11-15T10:00:00Z",
                    "location": "New Delhi",
                    "tag": "PickedUp",
                    "message": "Shipment picked up",
                }
            ]
        )

        event_record_id = self.create_and_claim_event(payload)

        with patch(
            "webhooks.webhook_event_processor.save_checkpoints_in_transaction",
            side_effect=RuntimeError("Synthetic checkpoint failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.process_claimed_webhook_event(
                    event_record_id,
                    self.WORKER_ID,
                )

        self.assertIsNone(
            self.get_shipment("TEST123456")
        )

        event = self.get_webhook_event(event_record_id)

        self.assertEqual(event["status"], "processing")

        self.assertEqual(
            self.get_table_count("checkpoints"),
            0,
        )

    def test_wrong_worker_cannot_process_claimed_event(self):
        payload = self.make_payload()

        event_record_id = self.create_and_claim_event(payload)

        with self.assertRaises(RuntimeError):
            self.process_claimed_webhook_event(
                event_record_id,
                "different-worker",
            )

        self.assertIsNone(
            self.get_shipment("TEST123456")
        )

        event = self.get_webhook_event(event_record_id)

        self.assertEqual(event["status"], "processing")

    # ---------------------------------------------------------
    # Scope safeguards
    # ---------------------------------------------------------


    def test_status_transition_enqueues_notification_in_webhook_transaction(self):
        # Establish shipment through the webhook processor first.
        _, first_result = self.process_payload(
            self.make_payload(status="InfoReceived", event_timestamp=1794825600)
        )
        shipment_id = first_result["shipment_id"]

        from database import schema
        connection = self.get_connection()
        try:
            schema.create_user_shipments_table(connection=connection)
            schema.create_notifications_table(connection=connection)
            connection.execute(
                "INSERT INTO users (external_user_id, created_at) VALUES (?, ?)",
                ("registered-user-1", "2026-11-16T00:00:00+00:00"),
            )
            user_id = connection.execute(
                "SELECT user_id FROM users WHERE external_user_id = ?",
                ("registered-user-1",),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO user_shipments (user_id, shipment_id, monitoring_enabled, created_at) "
                "VALUES (?, ?, 1, ?)",
                (user_id, shipment_id, "2026-11-16T00:00:00+00:00"),
            )
            connection.execute(
                """INSERT INTO notification_channel_preferences
                   (user_id, channel, recipient, enabled, verified_at, created_at, updated_at)
                   VALUES (?, 'telegram', '123456789', 1, ?, ?, ?)""",
                (user_id, "2026-11-16T00:00:00+00:00", "2026-11-16T00:00:00+00:00", "2026-11-16T00:00:00+00:00"),
            )
            connection.commit()
        finally:
            connection.close()

        _, update_result = self.process_payload(
            self.make_payload(status="InTransit", event_timestamp=1794912000)
        )
        self.assertEqual(update_result["notification_generation"]["status"], "generated")
        self.assertEqual(update_result["notification_generation"]["queued_or_already_queued"], 1)

        connection = self.get_connection()
        try:
            rows = connection.execute(
                "SELECT notification_type, recipient, previous_status, new_status, delivery_status "
                "FROM notifications"
            ).fetchall()
            self.assertEqual(rows, [("telegram", "123456789", "InfoReceived", "InTransit", "pending")])
        finally:
            connection.close()


    def test_notification_enqueue_failure_rolls_back_shipment_and_webhook_completion(self):
        event_record_id = self.create_and_claim_event(
            self.make_payload(status="InTransit")
        )
        from unittest.mock import patch

        def fail_inside_transaction(connection, payload):
            self.assertTrue(connection.in_transaction)
            raise RuntimeError("synthetic notification enqueue failure")

        with patch(
            "webhooks.webhook_event_processor.generate_notifications_in_transaction",
            side_effect=fail_inside_transaction,
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic notification enqueue failure"):
                self.process_claimed_webhook_event(event_record_id, self.WORKER_ID)

        self.assertIsNone(self.get_shipment("TEST123456"))
        event = self.get_webhook_event(event_record_id)
        self.assertEqual(event["status"], "processing")

    def test_same_status_update_does_not_enqueue_notification(self):
        _, first_result = self.process_payload(
            self.make_payload(status="InTransit", event_timestamp=1794825600)
        )
        connection = self.get_connection()
        try:
            from database import schema
            schema.create_notifications_table(connection=connection)
            connection.commit()
        finally:
            connection.close()

        _, second_result = self.process_payload(
            self.make_payload(status="InTransit", event_timestamp=1794912000)
        )
        self.assertIsNone(second_result["notification_generation"])

    def test_unknown_shipment_creation_does_not_create_user_or_notification(self):
        payload = self.make_payload()

        _, result = self.process_payload(payload)

        self.assertTrue(result["shipment_created"])

        self.assertEqual(
            self.get_table_count("users"),
            0,
        )

        self.assertEqual(
            self.get_table_count("user_shipments"),
            0,
        )

        self.assertEqual(
            self.get_table_count("notification_preferences"),
            0,
        )

        self.assertEqual(
            self.get_table_count("notifications"),
            0,
        )


if __name__ == "__main__":
    unittest.main()