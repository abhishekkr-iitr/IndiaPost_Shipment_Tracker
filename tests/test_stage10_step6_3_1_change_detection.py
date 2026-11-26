import sqlite3
import unittest

from notifications.change_detection import (
    ShipmentChangeDetectionError,
    detect_and_record_shipment_changes,
)


class SharedShipmentChangeDetectionTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("CREATE TABLE shipments (shipment_id INTEGER PRIMARY KEY)")
        self.connection.execute("INSERT INTO shipments VALUES (1)")
        self.connection.commit()
        self.connection.execute("""CREATE TABLE shipment_change_events (
            change_event_id TEXT PRIMARY KEY, shipment_id INTEGER NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN ('shipment.status_changed','shipment.checkpoint_added')),
            schema_version INTEGER NOT NULL CHECK(schema_version = 2),
            source TEXT NOT NULL CHECK(source IN ('webhook','monitoring')),
            idempotency_key TEXT NOT NULL UNIQUE, event_payload TEXT NOT NULL,
            occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(shipment_id) REFERENCES shipments(shipment_id) ON DELETE CASCADE)""")
        self.shipment = {
            "shipment_id": 1,
            "tracking_number": "AB123456789IN",
            "carrier": "india-post",
        }
        self.recorded_at = "2026-11-16T12:00:00+00:00"

    def tearDown(self):
        self.connection.close()

    def detect(self, **overrides):
        args = {
            "shipment": self.shipment,
            "previous_status": "Booked",
            "new_status": "In Transit",
            "source": "monitoring",
            "new_checkpoints": [],
            "status_occurred_at": "2026-11-16T11:00:00+00:00",
            "recorded_at": self.recorded_at,
        }
        args.update(overrides)
        return detect_and_record_shipment_changes(self.connection, **args)

    def test_status_transition_records_v2_event(self):
        result = self.detect()
        self.assertTrue(result["status_changed"])
        event = result["status_event"]
        self.assertEqual(event["event_type"], "shipment.status_changed")
        self.assertEqual(event["event_payload"]["change"], {
            "previous_status": "Booked", "new_status": "In Transit"
        })

    def test_unchanged_status_records_no_status_event(self):
        result = self.detect(previous_status="In Transit", new_status="In Transit",
                             status_occurred_at=None)
        self.assertFalse(result["status_changed"])
        self.assertIsNone(result["status_event"])

    def test_transition_without_occurrence_timestamp_is_rejected(self):
        with self.assertRaises(ShipmentChangeDetectionError):
            self.detect(status_occurred_at=None)

    def test_recurrent_same_status_pair_at_new_time_is_distinct(self):
        first = self.detect()
        second = self.detect(status_occurred_at="2026-11-16T12:30:00+00:00")
        self.assertNotEqual(first["status_event"]["idempotency_key"],
                            second["status_event"]["idempotency_key"])
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM shipment_change_events WHERE event_type='shipment.status_changed'"
        ).fetchone()[0], 2)

    def test_same_status_occurrence_deduplicates_across_sources(self):
        first = self.detect(source="webhook")
        second = self.detect(source="monitoring")
        self.assertTrue(first["status_event"]["inserted"])
        self.assertFalse(second["status_event"]["inserted"])
        self.assertEqual(first["status_event"]["change_event_id"],
                         second["status_event"]["change_event_id"])

    def test_cross_source_same_checkpoint_transition_deduplicates_with_different_timestamps(self):
        first = self.detect(source="webhook", status_occurred_at="2026-11-16T11:05:00+00:00",
                            status_checkpoint_id=41)
        second = self.detect(source="monitoring", status_occurred_at="2026-11-16T10:55:00+00:00",
                             status_checkpoint_id=41)
        self.assertTrue(first["status_event"]["inserted"])
        self.assertFalse(second["status_event"]["inserted"])
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM shipment_change_events WHERE event_type='shipment.status_changed'"
        ).fetchone()[0], 1)

    def test_distinct_checkpoint_identity_preserves_legitimate_recurrence(self):
        first = self.detect(status_checkpoint_id=41)
        self.detect(previous_status="In Transit", new_status="Booked",
                    status_occurred_at="2026-11-16T12:00:00+00:00", status_checkpoint_id=42)
        third = self.detect(status_checkpoint_id=43,
                            status_occurred_at="2026-11-16T13:00:00+00:00")
        self.assertNotEqual(first["status_event"]["idempotency_key"],
                            third["status_event"]["idempotency_key"])
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM shipment_change_events WHERE event_type='shipment.status_changed'"
        ).fetchone()[0], 3)

    def test_new_checkpoint_records_checkpoint_only_event(self):
        checkpoint = {
            "checkpoint_id": 41,
            "event_date": "2026-11-16T10:15:00+00:00",
            "location": "New Delhi",
            "status": "In Transit",
            "message": "Dispatched.",
        }
        result = self.detect(previous_status="In Transit", new_status="In Transit",
                             status_occurred_at=None, new_checkpoints=[checkpoint])
        self.assertFalse(result["status_changed"])
        self.assertEqual(result["checkpoint_events_recorded"], 1)
        self.assertEqual(result["checkpoint_events"][0]["event_type"],
                         "shipment.checkpoint_added")

    def test_same_checkpoint_deduplicates_across_sources(self):
        checkpoint = {"checkpoint_id": 41, "event_date": "2026-11-16T10:15:00+00:00",
                      "location": "New Delhi", "status": "In Transit", "message": "Dispatched."}
        first = self.detect(source="webhook", previous_status="In Transit",
                             new_status="In Transit", status_occurred_at=None,
                             new_checkpoints=[checkpoint])
        second = self.detect(source="monitoring", previous_status="In Transit",
                             new_status="In Transit", status_occurred_at=None,
                             new_checkpoints=[checkpoint])
        self.assertTrue(first["checkpoint_events"][0]["inserted"])
        self.assertFalse(second["checkpoint_events"][0]["inserted"])
        self.assertEqual(first["checkpoint_events"][0]["change_event_id"],
                         second["checkpoint_events"][0]["change_event_id"])

    def test_duplicate_checkpoint_ids_in_one_batch_are_processed_once(self):
        checkpoint = {"checkpoint_id": 41, "event_date": None, "location": "New Delhi"}
        result = self.detect(previous_status="In Transit", new_status="In Transit",
                             status_occurred_at=None,
                             new_checkpoints=[checkpoint, checkpoint])
        self.assertEqual(result["checkpoint_events_recorded"], 1)

    def test_checkpoint_without_provider_timestamp_uses_recorded_time(self):
        result = self.detect(previous_status="In Transit", new_status="In Transit",
                             status_occurred_at=None,
                             new_checkpoints=[{"checkpoint_id": 42, "event_date": None}])
        payload = result["checkpoint_events"][0]["event_payload"]
        self.assertEqual(payload["occurred_at"], "2026-11-16T12:00:00.000000+00:00")
        self.assertIsNone(payload["change"]["checkpoint_event_at"])

    def test_invalid_source_is_rejected(self):
        with self.assertRaises(ShipmentChangeDetectionError):
            self.detect(source="api")

    def test_invalid_checkpoint_id_is_rejected(self):
        with self.assertRaises(ShipmentChangeDetectionError):
            self.detect(previous_status="In Transit", new_status="In Transit",
                        status_occurred_at=None,
                        new_checkpoints=[{"checkpoint_id": 0}])

    def test_detector_does_not_commit_caller_transaction(self):
        self.connection.execute("BEGIN")
        self.detect()
        self.assertTrue(self.connection.in_transaction)
        self.connection.rollback()
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM shipment_change_events"
        ).fetchone()[0], 0)

    def test_status_event_and_checkpoint_events_share_transaction(self):
        self.connection.execute("BEGIN")
        self.detect(new_checkpoints=[{"checkpoint_id": 41, "event_date": None}])
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM shipment_change_events"
        ).fetchone()[0], 2)
        self.connection.rollback()
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM shipment_change_events"
        ).fetchone()[0], 0)

    def test_checkpoint_input_must_be_a_sequence_of_mappings(self):
        with self.assertRaises(ShipmentChangeDetectionError):
            self.detect(new_checkpoints="not-a-list")


if __name__ == "__main__":
    unittest.main()
