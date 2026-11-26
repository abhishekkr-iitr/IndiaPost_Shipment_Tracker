import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from database.notification_delivery_receipt_repository import record_provider_receipt
from notifications.receipts import ProviderReceipt
from notifications.receipt_state import get_effective_receipt_state


class ReceiptOrderingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "receipt_ordering.db")
        self.previous_db_path = os.environ.get("INDIAPOST_DB_PATH")
        os.environ["INDIAPOST_DB_PATH"] = self.db_path
        con = sqlite3.connect(self.db_path)
        con.execute("PRAGMA foreign_keys = ON")
        con.executescript("""
            CREATE TABLE notifications (notification_id INTEGER PRIMARY KEY);
            CREATE TABLE notification_delivery_attempts (
                attempt_id INTEGER PRIMARY KEY,
                notification_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                provider_message_id TEXT,
                FOREIGN KEY(notification_id) REFERENCES notifications(notification_id)
            );
            CREATE TABLE notification_delivery_receipts (
                receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                notification_id INTEGER NOT NULL,
                attempt_id INTEGER,
                provider TEXT NOT NULL,
                provider_event_id TEXT NOT NULL,
                provider_message_id TEXT NOT NULL,
                receipt_status TEXT NOT NULL CHECK (
                    receipt_status IN ('submitted','delivered','failed','read')
                ),
                provider_event_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                FOREIGN KEY(notification_id) REFERENCES notifications(notification_id),
                FOREIGN KEY(attempt_id) REFERENCES notification_delivery_attempts(attempt_id),
                UNIQUE(provider, provider_event_id)
            );
            INSERT INTO notifications VALUES (1);
            INSERT INTO notifications VALUES (2);
            INSERT INTO notification_delivery_attempts VALUES (10, 1, 'whatsapp', 'msg-1');
            INSERT INTO notification_delivery_attempts VALUES (20, 2, 'whatsapp', 'msg-2');
        """)
        con.commit()
        con.close()
        self.base = datetime(2026, 11, 19, 8, 0, tzinfo=timezone.utc)

    def tearDown(self):
        if self.previous_db_path is None:
            os.environ.pop("INDIAPOST_DB_PATH", None)
        else:
            os.environ["INDIAPOST_DB_PATH"] = self.previous_db_path
        self.temp_dir.cleanup()

    def add(self, status, minutes, event_id=None, attempt_id=10, notification_id=1):
        event = ProviderReceipt(
            provider="whatsapp",
            provider_event_id=event_id or f"evt-{status}-{minutes}-{attempt_id}",
            provider_message_id=f"msg-{1 if attempt_id == 10 else 2}",
            status=status,
            provider_event_at=self.base + timedelta(minutes=minutes),
            attempt_id=attempt_id,
        )
        return record_provider_receipt(notification_id, event)

    def state(self, notification_id=1, attempt_id=10):
        return get_effective_receipt_state(notification_id, attempt_id)

    def test_no_receipts_returns_empty_state(self):
        result = self.state()
        self.assertIsNone(result["status"])
        self.assertEqual(result["event_count"], 0)

    def test_normal_submitted_delivered_read_progression(self):
        self.add("submitted", 1)
        self.add("delivered", 2)
        self.add("read", 3)
        result = self.state()
        self.assertEqual(result["status"], "read")
        self.assertEqual(result["event_count"], 3)

    def test_late_older_submitted_event_does_not_regress_delivered(self):
        self.add("delivered", 3)
        self.add("submitted", 1)
        result = self.state()
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(result["ignored_event_count"], 0)

    def test_out_of_order_arrival_is_reconstructed_by_provider_event_time(self):
        self.add("read", 3)
        self.add("delivered", 2)
        self.add("submitted", 1)
        result = self.state()
        self.assertEqual(result["status"], "read")
        self.assertEqual(result["ignored_event_count"], 0)

    def test_failed_state_cannot_be_replaced_by_later_delivered(self):
        self.add("failed", 2)
        self.add("delivered", 3)
        result = self.state()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["ignored_event_count"], 1)

    def test_delivered_state_cannot_be_replaced_by_later_failed(self):
        self.add("delivered", 2)
        self.add("failed", 3)
        result = self.state()
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(result["ignored_event_count"], 1)

    def test_read_state_is_terminal(self):
        self.add("read", 2)
        self.add("delivered", 3)
        result = self.state()
        self.assertEqual(result["status"], "read")
        self.assertEqual(result["ignored_event_count"], 1)

    def test_same_timestamp_delivered_then_read_resolves_to_read(self):
        self.add("read", 2, event_id="evt-read")
        self.add("delivered", 2, event_id="evt-delivered")
        result = self.state()
        self.assertEqual(result["status"], "read")
        self.assertEqual(result["ambiguous_event_count"], 0)

    def test_same_timestamp_delivered_and_failed_is_ambiguous(self):
        self.add("delivered", 2, event_id="evt-delivered")
        self.add("failed", 2, event_id="evt-failed")
        result = self.state()
        self.assertIsNone(result["status"])
        self.assertEqual(result["ambiguous_event_count"], 2)

    def test_ambiguous_later_timestamp_does_not_overwrite_prior_state(self):
        self.add("submitted", 1)
        self.add("delivered", 2, event_id="evt-delivered")
        self.add("failed", 2, event_id="evt-failed")
        result = self.state()
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["ambiguous_event_count"], 2)

    def test_receipt_state_is_isolated_per_attempt(self):
        self.add("delivered", 2)
        self.add("failed", 2, attempt_id=20, notification_id=2)
        self.assertEqual(self.state()["status"], "delivered")
        self.assertEqual(self.state(2, 20)["status"], "failed")

    def test_attempt_must_belong_to_notification(self):
        with self.assertRaisesRegex(ValueError, "does not belong"):
            get_effective_receipt_state(2, 10)

    def test_invalid_identifiers_rejected(self):
        for notification_id, attempt_id in ((0, 10), (1, 0), (True, 10), (1, True)):
            with self.subTest(notification_id=notification_id, attempt_id=attempt_id):
                with self.assertRaises(ValueError):
                    get_effective_receipt_state(notification_id, attempt_id)


if __name__ == "__main__":
    unittest.main()
