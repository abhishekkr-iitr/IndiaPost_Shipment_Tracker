import json
import sqlite3
import unittest
from datetime import datetime, timezone

from database.shipment_change_event_repository import (
    ShipmentChangeEventRepositoryError, checkpoint_idempotency_key,
    get_shipment_change_event, get_shipment_change_event_by_key,
    list_shipment_change_events, record_shipment_change_event,
)


class ShipmentChangeEventRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("CREATE TABLE shipments (shipment_id INTEGER PRIMARY KEY)")
        self.connection.execute("INSERT INTO shipments VALUES (1)")
        self.connection.execute("INSERT INTO shipments VALUES (2)")
        self.connection.execute("""CREATE TABLE shipment_change_events (
            change_event_id TEXT PRIMARY KEY, shipment_id INTEGER NOT NULL,
            event_type TEXT NOT NULL, schema_version INTEGER NOT NULL,
            source TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
            event_payload TEXT NOT NULL, occurred_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY (shipment_id) REFERENCES shipments(shipment_id) ON DELETE CASCADE)""")
        self.connection.commit()
        self.occurred_at = "2026-11-16T10:00:00+00:00"
        self.recorded_at = "2026-11-16T10:01:00+00:00"

    def tearDown(self):
        self.connection.close()

    def payload(self, *, event_id="evt-1", source="monitoring", shipment_id=1,
                event_type="shipment.checkpoint_added", checkpoint_id=101,
                occurred_at=None, recorded_at=None):
        return {
            "event_id": event_id,
            "event_type": event_type,
            "schema_version": 2,
            "source": source,
            "occurred_at": occurred_at or self.occurred_at,
            "recorded_at": recorded_at or self.recorded_at,
            "shipment": {
                "shipment_id": shipment_id,
                "tracking_number": f"TRACK{shipment_id:07d}",
                "carrier": "india-post",
            },
            "change": (
                {"checkpoint_id": checkpoint_id, "checkpoint_event_at": self.occurred_at,
                 "location": "New Delhi", "status": "In Transit", "message": "Dispatched."}
                if event_type == "shipment.checkpoint_added"
                else {"previous_status": "Booked", "new_status": "In Transit"}
            ),
        }

    def record(self, key="shipment-checkpoint:v1:1:101", source="monitoring",
               payload=None, shipment_id=1, event_type="shipment.checkpoint_added",
               event_id="evt-1", change_event_id=None):
        payload = payload or self.payload(event_id=event_id, source=source,
                                          shipment_id=shipment_id, event_type=event_type)
        return record_shipment_change_event(
            self.connection, shipment_id=shipment_id, event_type=event_type,
            source=source, idempotency_key=key, event_payload=payload,
            occurred_at=payload["occurred_at"],
            recorded_at=payload["recorded_at"],
            change_event_id=change_event_id,
        )

    def test_insert_returns_new_event_and_validated_json_payload(self):
        result = self.record()
        self.assertTrue(result["inserted"])
        self.assertEqual(result["change_event_id"], "evt-1")
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["event_payload"]["change"]["checkpoint_id"], 101)

    def test_duplicate_key_returns_original_event_across_sources(self):
        first = self.record(source="monitoring")
        second_payload = self.payload(
            event_id="evt-webhook", source="webhook",
            recorded_at="2026-11-16T10:02:00+00:00",
        )
        second = self.record(source="webhook", payload=second_payload)
        self.assertTrue(first["inserted"])
        self.assertFalse(second["inserted"])
        self.assertEqual(second["change_event_id"], first["change_event_id"])
        self.assertEqual(second["source"], "monitoring")
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM shipment_change_events"
        ).fetchone()[0], 1)

    def test_lookup_by_id_and_key(self):
        created = self.record()
        self.assertEqual(
            get_shipment_change_event(self.connection, created["change_event_id"])["change_event_id"],
            created["change_event_id"],
        )
        self.assertEqual(
            get_shipment_change_event_by_key(self.connection, created["idempotency_key"])["change_event_id"],
            created["change_event_id"],
        )

    def test_list_events_in_event_time_order(self):
        self.record("key-1", event_id="evt-1")
        later = self.payload(event_id="evt-2", checkpoint_id=102,
                             occurred_at="2026-11-16T11:00:00+00:00")
        self.record("key-2", payload=later, event_id="evt-2")
        rows = list_shipment_change_events(self.connection, 1)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["event_payload"]["change"]["checkpoint_id"], 101)

    def test_checkpoint_key_is_stable_and_scoped(self):
        self.assertEqual(checkpoint_idempotency_key(1, 101), "shipment-checkpoint:v1:1:101")
        self.assertNotEqual(checkpoint_idempotency_key(1, 101), checkpoint_idempotency_key(2, 101))

    def test_rejects_invalid_contract_payload(self):
        bad = self.payload()
        del bad["shipment"]["carrier"]
        with self.assertRaises(ShipmentChangeEventRepositoryError):
            self.record(payload=bad)

    def test_rejects_payload_column_mismatches(self):
        bad = self.payload(source="webhook")
        with self.assertRaises(ShipmentChangeEventRepositoryError):
            self.record(payload=bad, source="monitoring")
        bad = self.payload(shipment_id=2)
        with self.assertRaises(ShipmentChangeEventRepositoryError):
            self.record(payload=bad, shipment_id=1)

    def test_rejects_non_v2_payload(self):
        bad = self.payload()
        bad["schema_version"] = 1
        with self.assertRaises(ShipmentChangeEventRepositoryError):
            self.record(payload=bad)

    def test_rejects_change_event_id_mismatch(self):
        with self.assertRaises(ShipmentChangeEventRepositoryError):
            self.record(change_event_id="different-id")

    def test_rejects_idempotency_collision_with_different_change(self):
        self.record()
        conflicting = self.payload(event_id="evt-other", checkpoint_id=999)
        with self.assertRaises(ShipmentChangeEventRepositoryError):
            self.record(payload=conflicting)

    def test_rejects_idempotency_collision_with_different_shipment(self):
        self.record()
        conflicting = self.payload(event_id="evt-other", shipment_id=2)
        with self.assertRaises(ShipmentChangeEventRepositoryError):
            self.record(payload=conflicting, shipment_id=2)

    def test_repository_does_not_commit_caller_transaction(self):
        self.connection.execute("BEGIN")
        self.record()
        self.assertTrue(self.connection.in_transaction)
        self.connection.rollback()
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM shipment_change_events"
        ).fetchone()[0], 0)

    def test_foreign_key_rejects_unknown_shipment(self):
        payload = self.payload(shipment_id=999)
        with self.assertRaises(sqlite3.IntegrityError):
            self.record(payload=payload, shipment_id=999)

    def test_status_transition_v2_event_is_accepted(self):
        payload = self.payload(
            event_id="status-evt", event_type="shipment.status_changed"
        )
        result = self.record(
            key="status-transition:unique-1", payload=payload,
            event_type="shipment.status_changed", event_id="status-evt",
        )
        self.assertEqual(result["event_type"], "shipment.status_changed")


if __name__ == "__main__":
    unittest.main()
