"""Cross-component receipt integration tests for Stage 11 Step 10 Activity 6."""
from datetime import datetime, timedelta, timezone
import unittest

from database.db_connection import get_db_connection
from notifications.delivery_diagnostics import get_delivery_diagnostics
from notifications.receipt_processor import process_provider_receipt
from notifications.receipt_state import get_effective_receipt_state
from notifications.receipts import (
    ProviderReceipt,
    ReceiptCapabilities,
    ReceiptCapabilityRegistry,
)
try:
    import tests.test_notification_worker as _worker_fixture
except ImportError:
    try:
        import test.test_notification_worker as _worker_fixture
    except ImportError:
        import test_notification_worker as _worker_fixture


class ProviderReceiptIntegrationTests(unittest.TestCase):
    def setUp(self):
        _worker_fixture.WorkerFixture.setUp(self)
        self.notification_id = self.add_notification(
            key="receipt-integration", channel="email", recipient="person@example.com"
        )
        self.attempt_id = 7001
        connection = get_db_connection()
        connection.execute(
            """INSERT INTO notification_delivery_attempts (
                   attempt_id, notification_id, attempt_number, worker_id,
                   channel, provider, started_at, completed_at, outcome,
                   provider_message_id, error_message
               ) VALUES (?, ?, 1, 'receipt-integration-worker', 'email',
                         'example-provider', '2026-11-19T08:00:00+00:00',
                         '2026-11-19T08:00:01+00:00', 'sent',
                         'provider-message-7001', NULL)""",
            (self.attempt_id, self.notification_id),
        )
        connection.commit()
        connection.close()

        self.registry = ReceiptCapabilityRegistry()
        self.registry.register(
            "example-provider",
            ReceiptCapabilities(delivery_receipts=True, read_receipts=True),
        )
        self.base_time = datetime(2026, 11, 19, 8, 10, tzinfo=timezone.utc)

    def tearDown(self):
        _worker_fixture.WorkerFixture.tearDown(self)

    def add_notification(self, *args, **kwargs):
        return _worker_fixture.WorkerFixture.add_notification(self, *args, **kwargs)

    def _receipt(self, event_id, status, minutes, **overrides):
        values = dict(
            provider="example-provider",
            provider_event_id=event_id,
            provider_message_id="provider-message-7001",
            status=status,
            provider_event_at=self.base_time + timedelta(minutes=minutes),
            attempt_id=self.attempt_id,
        )
        values.update(overrides)
        return ProviderReceipt(**values)

    def _snapshot_delivery_state(self):
        connection = get_db_connection()
        try:
            notification = connection.execute(
                """SELECT delivery_status, attempt_count, next_retry_at,
                          claimed_by, lease_expires_at
                   FROM notifications WHERE notification_id = ?""",
                (self.notification_id,),
            ).fetchone()
            attempt = connection.execute(
                """SELECT outcome, provider_message_id, error_message
                   FROM notification_delivery_attempts WHERE attempt_id = ?""",
                (self.attempt_id,),
            ).fetchone()
            return notification, attempt
        finally:
            connection.close()

    def test_end_to_end_receipts_flow_through_processing_state_and_diagnostics(self):
        before = self._snapshot_delivery_state()
        for event_id, status, minute in (
            ("evt-submitted", "submitted", 1),
            ("evt-delivered", "delivered", 2),
            ("evt-read", "read", 3),
        ):
            result = process_provider_receipt(
                self.notification_id,
                self._receipt(event_id, status, minute),
                self.registry,
            )
            self.assertTrue(result["recorded"])
            self.assertFalse(result["duplicate"])

        state = get_effective_receipt_state(self.notification_id, self.attempt_id)
        self.assertEqual(state["status"], "read")
        self.assertEqual(state["event_count"], 3)

        diagnostics = get_delivery_diagnostics()["receipt_diagnostics"]
        self.assertEqual(diagnostics["total_events"], 3)
        self.assertEqual(diagnostics["events_by_status"]["submitted"], 1)
        self.assertEqual(diagnostics["events_by_status"]["delivered"], 1)
        self.assertEqual(diagnostics["events_by_status"]["read"], 1)
        self.assertEqual(diagnostics["attempts_with_receipts"], 1)
        self.assertEqual(diagnostics["effective_states_by_attempt"]["read"], 1)
        self.assertEqual(self._snapshot_delivery_state(), before)

    def test_end_to_end_out_of_order_arrival_keeps_provider_time_projection(self):
        for event_id, status, minute in (
            ("evt-read-late-arrival", "read", 3),
            ("evt-submitted-late-arrival", "submitted", 1),
            ("evt-delivered-late-arrival", "delivered", 2),
        ):
            process_provider_receipt(
                self.notification_id,
                self._receipt(event_id, status, minute),
                self.registry,
            )

        state = get_effective_receipt_state(self.notification_id, self.attempt_id)
        self.assertEqual(state["status"], "read")
        self.assertEqual(state["event_count"], 3)
        self.assertEqual(state["ignored_event_count"], 0)

    def test_end_to_end_duplicate_delivery_is_idempotent_in_history_and_diagnostics(self):
        receipt = self._receipt("evt-duplicate", "delivered", 2)
        first = process_provider_receipt(self.notification_id, receipt, self.registry)
        second = process_provider_receipt(self.notification_id, receipt, self.registry)

        self.assertTrue(first["recorded"])
        self.assertTrue(second["duplicate"])
        self.assertFalse(second["recorded"])
        report = get_delivery_diagnostics()["receipt_diagnostics"]
        self.assertEqual(report["total_events"], 1)
        self.assertEqual(report["events_by_status"]["delivered"], 1)

    def test_conflicting_duplicate_is_rejected_without_changing_history(self):
        original = self._receipt("evt-conflict", "delivered", 2)
        process_provider_receipt(self.notification_id, original, self.registry)

        with self.assertRaisesRegex(ValueError, "conflicts"):
            process_provider_receipt(
                self.notification_id,
                self._receipt("evt-conflict", "failed", 2),
                self.registry,
            )

        state = get_effective_receipt_state(self.notification_id, self.attempt_id)
        self.assertEqual(state["status"], "delivered")
        self.assertEqual(state["event_count"], 1)

    def test_unsupported_receipt_capability_is_rejected_before_persistence(self):
        restricted = ReceiptCapabilityRegistry()
        restricted.register(
            "example-provider",
            ReceiptCapabilities(delivery_receipts=False, read_receipts=False),
        )
        with self.assertRaisesRegex(ValueError, "does not support delivery"):
            process_provider_receipt(
                self.notification_id,
                self._receipt("evt-unsupported", "delivered", 2),
                restricted,
            )

        diagnostics = get_delivery_diagnostics()["receipt_diagnostics"]
        self.assertEqual(diagnostics["total_events"], 0)

    def test_receipt_message_mismatch_is_rejected_without_delivery_side_effects(self):
        before = self._snapshot_delivery_state()
        with self.assertRaisesRegex(ValueError, "message ID"):
            process_provider_receipt(
                self.notification_id,
                self._receipt(
                    "evt-wrong-message", "delivered", 2,
                    provider_message_id="not-the-attempt-message",
                ),
                self.registry,
            )
        self.assertEqual(self._snapshot_delivery_state(), before)
        self.assertEqual(
            get_delivery_diagnostics()["receipt_diagnostics"]["total_events"], 0
        )

    def test_receipt_events_remain_attempt_scoped_when_notification_has_multiple_attempts(self):
        second_attempt_id = 7002
        connection = get_db_connection()
        connection.execute(
            """INSERT INTO notification_delivery_attempts (
                   attempt_id, notification_id, attempt_number, worker_id,
                   channel, provider, started_at, completed_at, outcome,
                   provider_message_id, error_message
               ) VALUES (?, ?, 2, 'receipt-integration-worker', 'email',
                         'example-provider', '2026-11-19T08:01:00+00:00',
                         '2026-11-19T08:01:01+00:00', 'sent',
                         'provider-message-7002', NULL)""",
            (second_attempt_id, self.notification_id),
        )
        connection.commit()
        connection.close()

        process_provider_receipt(
            self.notification_id,
            self._receipt("evt-attempt-1", "delivered", 2),
            self.registry,
        )
        process_provider_receipt(
            self.notification_id,
            self._receipt(
                "evt-attempt-2", "failed", 3,
                provider_message_id="provider-message-7002",
                attempt_id=second_attempt_id,
            ),
            self.registry,
        )

        self.assertEqual(
            get_effective_receipt_state(self.notification_id, self.attempt_id)["status"],
            "delivered",
        )
        self.assertEqual(
            get_effective_receipt_state(self.notification_id, second_attempt_id)["status"],
            "failed",
        )
        diagnostics = get_delivery_diagnostics()["receipt_diagnostics"]
        self.assertEqual(diagnostics["attempts_with_receipts"], 2)
        self.assertEqual(diagnostics["effective_states_by_attempt"]["delivered"], 1)
        self.assertEqual(diagnostics["effective_states_by_attempt"]["failed"], 1)


if __name__ == "__main__":
    unittest.main()
