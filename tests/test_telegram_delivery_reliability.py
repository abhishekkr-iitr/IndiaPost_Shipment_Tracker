"""Reliability and recovery tests for the existing durable delivery worker.

Uses WorkerFixture's disposable SQLite database and fake adapters. No network
calls, live credentials, or production database are used.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from database.db_connection import get_db_connection
from database.notification_delivery_repository import claim_due_notifications
from notifications.delivery import AdapterRegistry, DeliveryResult
from notifications.worker import NotificationWorker
try:
    from tests.test_notification_worker import WorkerFixture
except ImportError:
    try:
        from test.test_notification_worker import WorkerFixture
    except ImportError:
        from test_notification_worker import WorkerFixture


class RecordingRetryAdapter:
    channel = "telegram"

    def __init__(self, result):
        self.result = result
        self.calls = []

    def deliver(self, notification):
        self.calls.append(notification["notification_id"])
        return self.result


class DeliveryReliabilityTests(WorkerFixture):
    def test_maximum_attempts_are_enforced(self):
        notification_id = self.add_notification()
        adapter = RecordingRetryAdapter(
            DeliveryResult("retryable", error_code="temporary", error_message="Try later.")
        )
        registry = AdapterRegistry()
        registry.register(adapter)
        worker = NotificationWorker(
            "retry-worker", registry, max_attempts=2, retry_delay_seconds=0
        )

        first = worker.run_once()
        second = worker.run_once()
        third = worker.run_once()

        self.assertEqual(first["retry_scheduled"], 1)
        self.assertEqual(second["failed"], 1)
        self.assertEqual(third["claimed"], 0)
        self.assertEqual(adapter.calls, [notification_id, notification_id])

        connection = get_db_connection()
        row = connection.execute(
            "SELECT delivery_status, attempt_count, claimed_by, next_retry_at "
            "FROM notifications WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()
        connection.close()
        self.assertEqual(row, ("failed", 2, None, None))

    def test_expired_worker_lease_allows_recovery(self):
        notification_id = self.add_notification()
        first_claim = claim_due_notifications("worker-crashed", lease_seconds=120)
        self.assertEqual(len(first_claim), 1)

        connection = get_db_connection()
        expired = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        connection.execute(
            "UPDATE notifications SET lease_expires_at = ? WHERE notification_id = ?",
            (expired, notification_id),
        )
        connection.commit()
        connection.close()

        recovered = claim_due_notifications("recovery-worker", lease_seconds=120)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["notification_id"], notification_id)
        self.assertEqual(recovered[0]["attempt_count"], 2)
        self.assertEqual(recovered[0]["claimed_by"], "recovery-worker")

    def test_provider_acceptance_before_persistence_failure_can_duplicate(self):
        notification_id = self.add_notification()
        adapter = RecordingRetryAdapter(DeliveryResult("sent", provider_message_id="tg-accepted-1"))
        registry = AdapterRegistry()
        registry.register(adapter)
        worker = NotificationWorker("worker-a", registry, lease_seconds=120)

        # Simulate Telegram accepting the request, followed by a local DB
        # persistence failure before the worker can record success.
        with patch("notifications.worker.complete_notification",
                   side_effect=RuntimeError("simulated persistence outage")):
            with self.assertRaisesRegex(RuntimeError, "simulated persistence outage"):
                worker.run_once()

        connection = get_db_connection()
        row = connection.execute(
            "SELECT delivery_status, attempt_count, claimed_by FROM notifications "
            "WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()
        expired = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        connection.execute(
            "UPDATE notifications SET lease_expires_at = ? WHERE notification_id = ?",
            (expired, notification_id),
        )
        connection.commit()
        connection.close()
        self.assertEqual(row, ("pending", 1, "worker-a"))

        # A later worker reclaims and sends again: this documents at-least-once
        # delivery and the unavoidable duplicate window without provider-side
        # idempotency support.
        result = NotificationWorker("worker-b", registry, lease_seconds=120).run_once()
        self.assertEqual(result["sent"], 1)
        self.assertEqual(adapter.calls, [notification_id, notification_id])

        connection = get_db_connection()
        final_row = connection.execute(
            "SELECT delivery_status, attempt_count, provider_message_id, claimed_by "
            "FROM notifications WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()
        connection.close()
        self.assertEqual(final_row, ("sent", 2, "tg-accepted-1", None))


if __name__ == "__main__":
    import unittest
    unittest.main()
