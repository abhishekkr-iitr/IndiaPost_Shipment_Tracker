import hashlib
import json
import sqlite3
import unittest

from database.checkpoint_repository import (
    generate_checkpoint_fingerprint,
    save_checkpoints_in_transaction,
)


class CheckpointTimestampIdentityTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("""
            CREATE TABLE checkpoints (
                checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
                shipment_id INTEGER NOT NULL,
                event_date TEXT,
                location TEXT,
                status TEXT,
                message TEXT,
                event_fingerprint TEXT NOT NULL,
                duration_since_previous_seconds INTEGER,
                created_at TEXT NOT NULL,
                UNIQUE (shipment_id, event_fingerprint)
            )
        """)

    def tearDown(self):
        self.connection.close()

    def _save(self, records):
        return save_checkpoints_in_transaction(
            self.connection, 1, records
        )

    def _count(self):
        return self.connection.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE shipment_id = 1"
        ).fetchone()[0]

    def test_z_and_utc_offset_timestamps_share_fingerprint(self):
        a = {"date":"2026-11-16T10:00:00Z","location":"Delhi",
             "status":"InTransit","message":"Departed"}
        b = dict(a, date="2026-11-16T10:00:00+00:00")
        self.assertEqual(generate_checkpoint_fingerprint(a),
                         generate_checkpoint_fingerprint(b))
        self.assertEqual(self._save([a]), 1)
        self.assertEqual(self._save([b]), 0)
        self.assertEqual(self._count(), 1)

    def test_equivalent_nonzero_offsets_are_deduplicated(self):
        a = {"date":"2026-11-16T15:30:00+05:30","location":"Delhi",
             "status":"InTransit","message":"Departed"}
        b = dict(a, date="2026-11-16T10:00:00Z")
        self.assertEqual(self._save([a]), 1)
        self.assertEqual(self._save([b]), 0)
        self.assertEqual(self._count(), 1)

    def test_legacy_fingerprint_row_is_matched_by_normalized_identity(self):
        legacy = {"date":"2026-11-16T10:00:00Z","location":"Delhi",
                  "status":"InTransit","message":"Departed"}
        old_json = json.dumps(legacy, sort_keys=True)
        old_fingerprint = hashlib.sha256(old_json.encode("utf-8")).hexdigest()
        self.connection.execute("""
            INSERT INTO checkpoints
            (shipment_id,event_date,location,status,message,event_fingerprint,
             duration_since_previous_seconds,created_at)
            VALUES (1,?,?,?,?,?,NULL,'test')
        """, (legacy["date"], legacy["location"], legacy["status"],
              legacy["message"], old_fingerprint))
        self.connection.commit()

        incoming = dict(legacy, date="2026-11-16T10:00:00+00:00")
        self.assertEqual(self._save([incoming]), 0)
        self.assertEqual(self._count(), 1)

    def test_distinct_checkpoint_attributes_are_preserved(self):
        base = {"date":"2026-11-16T10:00:00Z","location":"Delhi",
                "status":"InTransit","message":"Departed"}
        distinct = [
            dict(base, location="Mumbai"),
            dict(base, status="Arrived"),
            dict(base, message="Different event"),
        ]
        self.assertEqual(self._save([base]), 1)
        self.assertEqual(self._save(distinct), 3)
        self.assertEqual(self._count(), 4)

    def test_naive_timestamp_retains_utc_interpretation(self):
        naive = {"date":"2026-11-16T10:00:00","location":"Delhi",
                 "status":"InTransit","message":"Departed"}
        aware = dict(naive, date="2026-11-16T10:00:00Z")
        self.assertEqual(self._save([naive]), 1)
        self.assertEqual(self._save([aware]), 0)
        self.assertEqual(self._count(), 1)

    def test_invalid_timestamp_preserves_raw_value_identity(self):
        a = {"date":"not-a-date","location":"Delhi",
             "status":"InTransit","message":"Departed"}
        b = dict(a)
        self.assertEqual(self._save([a]), 1)
        self.assertEqual(self._save([b]), 0)
        self.assertEqual(self._count(), 1)


if __name__ == "__main__":
    unittest.main()
