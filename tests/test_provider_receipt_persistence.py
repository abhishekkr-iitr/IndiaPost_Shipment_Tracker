import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

from database.notification_delivery_receipt_repository import record_provider_receipt
from notifications.receipts import (
    ProviderReceipt, ReceiptCapabilities, ReceiptCapabilityRegistry,
)
from notifications.receipt_processor import process_provider_receipt


class ReceiptPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "receipts.db")
        self.previous_db_path = os.environ.get("INDIAPOST_DB_PATH")
        os.environ["INDIAPOST_DB_PATH"] = self.db_path
        connection = sqlite3.connect(self.db_path)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript("""
            CREATE TABLE notifications (
                notification_id INTEGER PRIMARY KEY
            );
            CREATE TABLE notification_delivery_attempts (
                attempt_id INTEGER PRIMARY KEY,
                notification_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                provider_message_id TEXT,
                FOREIGN KEY(notification_id)
                    REFERENCES notifications(notification_id) ON DELETE CASCADE
            );
            CREATE TABLE notification_delivery_receipts (
                receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                notification_id INTEGER NOT NULL,
                attempt_id INTEGER,
                provider TEXT NOT NULL,
                provider_event_id TEXT NOT NULL,
                provider_message_id TEXT NOT NULL,
                receipt_status TEXT NOT NULL CHECK (
                    receipt_status IN ('submitted', 'delivered', 'failed', 'read')
                ),
                provider_event_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                FOREIGN KEY(notification_id)
                    REFERENCES notifications(notification_id) ON DELETE CASCADE,
                FOREIGN KEY(attempt_id)
                    REFERENCES notification_delivery_attempts(attempt_id) ON DELETE SET NULL,
                UNIQUE(provider, provider_event_id)
            );
            INSERT INTO notifications(notification_id) VALUES (1);
            INSERT INTO notification_delivery_attempts(
                attempt_id, notification_id, provider, provider_message_id
            ) VALUES (10, 1, 'whatsapp', 'msg-1');
        """)
        connection.commit()
        connection.close()

    def tearDown(self):
        if self.previous_db_path is None:
            os.environ.pop("INDIAPOST_DB_PATH", None)
        else:
            os.environ["INDIAPOST_DB_PATH"] = self.previous_db_path
        self.temp_dir.cleanup()

    def receipt(self, **kwargs):
        values = dict(
            provider="whatsapp",
            provider_event_id="evt-1",
            provider_message_id="msg-1",
            status="delivered",
            provider_event_at=datetime.now(timezone.utc),
            attempt_id=10,
        )
        values.update(kwargs)
        return ProviderReceipt(**values)

    def test_receipt_is_persisted_and_correlated(self):
        result = record_provider_receipt(1, self.receipt())
        self.assertTrue(result["recorded"])
        self.assertFalse(result["duplicate"])
        connection = sqlite3.connect(self.db_path)
        row = connection.execute(
            """SELECT notification_id, attempt_id, provider, provider_event_id,
                      provider_message_id, receipt_status
               FROM notification_delivery_receipts"""
        ).fetchone()
        connection.close()
        self.assertEqual(row, (1, 10, "whatsapp", "evt-1", "msg-1", "delivered"))

    def test_duplicate_provider_event_is_idempotent(self):
        first = record_provider_receipt(1, self.receipt())
        second = record_provider_receipt(1, self.receipt())
        self.assertTrue(first["recorded"])
        self.assertFalse(second["recorded"])
        self.assertTrue(second["duplicate"])
        connection = sqlite3.connect(self.db_path)
        count = connection.execute(
            "SELECT COUNT(*) FROM notification_delivery_receipts"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(count, 1)

    def test_unknown_notification_rejected(self):
        with self.assertRaises(ValueError):
            record_provider_receipt(999, self.receipt())

    def test_attempt_must_belong_to_notification(self):
        with self.assertRaises(ValueError):
            record_provider_receipt(1, self.receipt(attempt_id=999))

    def test_attempt_provider_must_match(self):
        with self.assertRaises(ValueError):
            record_provider_receipt(1, self.receipt(provider="sms"))

    def test_attempt_message_id_must_match(self):
        with self.assertRaises(ValueError):
            record_provider_receipt(1, self.receipt(provider_message_id="other"))

    def test_conflicting_duplicate_event_rejected(self):
        record_provider_receipt(1, self.receipt())
        with self.assertRaises(ValueError):
            record_provider_receipt(
                1, self.receipt(provider_message_id="different")
            )

    def registry(self, delivery=True, read=True):
        registry = ReceiptCapabilityRegistry()
        registry.register(
            "whatsapp",
            ReceiptCapabilities(delivery_receipts=delivery, read_receipts=read),
        )
        return registry

    def test_processor_enforces_provider_delivery_capability(self):
        with self.assertRaisesRegex(ValueError, "does not support delivery"):
            process_provider_receipt(1, self.receipt(), self.registry(delivery=False, read=False))

    def test_processor_enforces_read_capability_separately(self):
        with self.assertRaisesRegex(ValueError, "does not support read"):
            process_provider_receipt(
                1, self.receipt(status="read"), self.registry(delivery=True, read=False)
            )

    def test_processor_requires_explicit_registration(self):
        with self.assertRaises(LookupError):
            process_provider_receipt(1, self.receipt(), ReceiptCapabilityRegistry())

    def test_processor_correlates_attempt_when_attempt_id_missing(self):
        result = process_provider_receipt(
            1, self.receipt(attempt_id=None), self.registry()
        )
        self.assertTrue(result["recorded"])
        connection = sqlite3.connect(self.db_path)
        attempt_id = connection.execute(
            "SELECT attempt_id FROM notification_delivery_receipts"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(attempt_id, 10)

    def test_processor_rejects_unmatched_attempt(self):
        with self.assertRaisesRegex(ValueError, "could not be correlated"):
            process_provider_receipt(
                1, self.receipt(provider_message_id="unknown", attempt_id=None),
                self.registry(),
            )

    def test_processor_rejects_ambiguous_attempt(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            """INSERT INTO notification_delivery_attempts
               (attempt_id, notification_id, provider, provider_message_id)
               VALUES (11, 1, 'whatsapp', 'msg-1')"""
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(ValueError, "multiple delivery attempts"):
            process_provider_receipt(
                1, self.receipt(attempt_id=None), self.registry()
            )

    def test_identical_duplicate_is_idempotent_through_processor(self):
        first = process_provider_receipt(1, self.receipt(), self.registry())
        second = process_provider_receipt(1, self.receipt(), self.registry())
        self.assertTrue(first["recorded"])
        self.assertTrue(second["duplicate"])

    def test_duplicate_event_with_changed_status_is_rejected(self):
        process_provider_receipt(1, self.receipt(), self.registry())
        with self.assertRaisesRegex(ValueError, "conflicts"):
            process_provider_receipt(
                1, self.receipt(status="failed"), self.registry()
            )

    def test_duplicate_event_with_changed_timestamp_is_rejected(self):
        process_provider_receipt(1, self.receipt(), self.registry())
        changed_time = datetime(2026, 11, 18, tzinfo=timezone.utc)
        with self.assertRaisesRegex(ValueError, "conflicts"):
            process_provider_receipt(
                1, self.receipt(provider_event_at=changed_time), self.registry()
            )


if __name__ == "__main__":
    unittest.main()
