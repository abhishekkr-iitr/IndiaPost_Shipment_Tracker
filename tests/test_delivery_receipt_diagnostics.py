import unittest
from datetime import datetime, timedelta, timezone

from database.db_connection import get_db_connection
from database.notification_delivery_receipt_repository import record_provider_receipt
from notifications.delivery_diagnostics import get_delivery_diagnostics
from notifications.receipts import ProviderReceipt
try:
    from tests.test_notification_worker import WorkerFixture
except ImportError:
    try:
        from test.test_notification_worker import WorkerFixture
    except ImportError:
        from test_notification_worker import WorkerFixture


class DeliveryReceiptDiagnosticsTests(WorkerFixture):
    def _add_attempt(self, notification_id, attempt_id=101, message_id="provider-msg-1"):
        connection = get_db_connection()
        connection.execute(
            """INSERT INTO notification_delivery_attempts (
                   attempt_id, notification_id, attempt_number, worker_id,
                   channel, provider, started_at, completed_at, outcome,
                   provider_message_id, error_message
               ) VALUES (?, ?, 1, 'worker-test', 'email', 'example-provider',
                         '2026-11-19T08:00:00+00:00',
                         '2026-11-19T08:00:01+00:00', 'sent', ?, NULL)""",
            (attempt_id, notification_id, message_id),
        )
        connection.commit()
        connection.close()

    def _receipt(self, event_id, status, minute, attempt_id=101, message_id="provider-msg-1"):
        return ProviderReceipt(
            provider="example-provider",
            provider_event_id=event_id,
            provider_message_id=message_id,
            status=status,
            provider_event_at=datetime(2026, 11, 19, 8, minute, tzinfo=timezone.utc),
            attempt_id=attempt_id,
        )

    def test_empty_database_includes_zeroed_receipt_diagnostics(self):
        report = get_delivery_diagnostics()
        self.assertEqual(report["receipt_diagnostics"], {
            "total_events": 0,
            "events_by_status": {
                "submitted": 0, "delivered": 0, "failed": 0, "read": 0,
            },
            "events_without_delivery_attempt": 0,
            "attempts_with_receipts": 0,
            "effective_states_by_attempt": {
                "submitted": 0, "delivered": 0, "failed": 0, "read": 0,
                "unresolved_or_ambiguous": 0,
            },
        })

    def test_reports_event_counts_and_effective_state_per_attempt(self):
        notification_id = self.add_notification("receipt-diagnostics")
        self._add_attempt(notification_id)
        record_provider_receipt(
            notification_id, self._receipt("evt-submitted", "submitted", 1)
        )
        record_provider_receipt(
            notification_id, self._receipt("evt-delivered", "delivered", 2)
        )

        report = get_delivery_diagnostics()["receipt_diagnostics"]
        self.assertEqual(report["total_events"], 2)
        self.assertEqual(report["events_by_status"], {
            "submitted": 1, "delivered": 1, "failed": 0, "read": 0,
        })
        self.assertEqual(report["attempts_with_receipts"], 1)
        self.assertEqual(report["effective_states_by_attempt"]["delivered"], 1)
        self.assertEqual(report["effective_states_by_attempt"]["submitted"], 0)

    def test_reports_unassociated_receipt_events_without_exposing_identifiers(self):
        notification_id = self.add_notification("unassociated-receipt")
        record_provider_receipt(
            notification_id,
            ProviderReceipt(
                provider="example-provider",
                provider_event_id="secret-event-id",
                provider_message_id="secret-message-id",
                status="submitted",
                provider_event_at=datetime(2026, 11, 19, 8, 1, tzinfo=timezone.utc),
            ),
        )

        report = get_delivery_diagnostics()["receipt_diagnostics"]
        self.assertEqual(report["events_without_delivery_attempt"], 1)
        serialized = repr(report)
        self.assertNotIn("secret-event-id", serialized)
        self.assertNotIn("secret-message-id", serialized)

    def test_diagnostics_do_not_modify_receipt_history_or_delivery_state(self):
        notification_id = self.add_notification("read-only-receipt-diagnostics")
        self._add_attempt(notification_id)
        record_provider_receipt(
            notification_id, self._receipt("evt-readonly", "delivered", 2)
        )
        connection = get_db_connection()
        before = (
            connection.execute(
                "SELECT COUNT(*) FROM notification_delivery_receipts"
            ).fetchone()[0],
            connection.execute(
                "SELECT delivery_status, attempt_count FROM notifications WHERE notification_id = ?",
                (notification_id,),
            ).fetchone(),
            connection.execute(
                "SELECT outcome FROM notification_delivery_attempts WHERE attempt_id = 101"
            ).fetchone(),
        )
        connection.close()

        get_delivery_diagnostics()

        connection = get_db_connection()
        after = (
            connection.execute(
                "SELECT COUNT(*) FROM notification_delivery_receipts"
            ).fetchone()[0],
            connection.execute(
                "SELECT delivery_status, attempt_count FROM notifications WHERE notification_id = ?",
                (notification_id,),
            ).fetchone(),
            connection.execute(
                "SELECT outcome FROM notification_delivery_attempts WHERE attempt_id = 101"
            ).fetchone(),
        )
        connection.close()
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
