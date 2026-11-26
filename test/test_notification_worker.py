import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from database import schema
from database.migrations import LATEST_SCHEMA_VERSION
from database.migrations import apply_migrations
from database.db_connection import get_db_connection
from database.notification_delivery_repository import claim_due_notifications
from database.notification_channel_preference_repository import save_channel_preference
from notifications.delivery import AdapterRegistry, DeliveryResult
from notifications.worker import NotificationWorker


class FakeAdapter:
    channel = "telegram"
    def __init__(self, result=None):
        self.result = result or DeliveryResult("sent", provider_message_id="tg-42")
        self.calls = []
    def deliver(self, notification):
        self.calls.append(notification)
        return self.result


class WorkerFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "worker.sqlite3")
        self.env = patch.dict(os.environ, {"INDIAPOST_DB_PATH": self.db_path})
        self.env.start()
        connection = get_db_connection()
        schema.create_users_table(connection)
        schema.create_shipments_table(connection)
        schema.create_user_shipments_table(connection)
        schema.create_checkpoints_table(connection)
        schema.create_notification_preferences_table(connection)
        schema.create_notifications_table(connection)
        connection.execute("INSERT INTO users(external_user_id, created_at) VALUES ('u1', '2026-01-01T00:00:00+00:00')")
        connection.execute("INSERT INTO shipments(tracking_number, carrier, provider, created_at, updated_at) VALUES ('AB123456789IN', 'india_post', 'aftership', '2026-01-01', '2026-01-01')")
        connection.execute("INSERT INTO user_shipments(user_id, shipment_id, created_at) VALUES (1, 1, '2026-01-01')")
        connection.commit()
        connection.close()
        apply_migrations()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def add_notification(self, key="k1", channel="telegram", recipient="123456789"):
        verified = datetime.now(timezone.utc).isoformat()
        save_channel_preference(1, channel, recipient, True, verified)
        connection = get_db_connection()
        cursor = connection.execute(
            """INSERT INTO notifications
               (user_shipment_id, notification_type, recipient, notification_message,
                delivery_status, idempotency_key, created_at)
               VALUES (1, ?, ?, 'Shipment delivered.', 'pending', ?, '2026-01-01')""",
            (channel, recipient, key),
        )
        notification_id = cursor.lastrowid
        connection.commit()
        connection.close()
        return notification_id

    def test_migration_adds_delivery_columns_without_changing_existing_rows(self):
        notification_id = self.add_notification()
        result = apply_migrations()
        self.assertEqual(result["current_version"], LATEST_SCHEMA_VERSION)
        connection = get_db_connection()
        row = connection.execute("SELECT * FROM notifications WHERE notification_id = ?", (notification_id,)).fetchone()
        columns = [item[1] for item in connection.execute("PRAGMA table_info(notifications)")]
        data = dict(zip(columns, row))
        self.assertEqual(data["idempotency_key"], "k1")
        self.assertEqual(data["delivery_status"], "pending")
        self.assertIsNone(data["claimed_by"])
        self.assertIsNone(data["next_retry_at"])
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        connection.close()

    def test_claim_is_exclusive_and_increments_attempt(self):
        self.add_notification()
        first = claim_due_notifications("worker-a")
        second = claim_due_notifications("worker-b")
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(first[0]["attempt_count"], 1)
        self.assertEqual(first[0]["claimed_by"], "worker-a")

    def test_expired_lease_can_be_reclaimed(self):
        self.add_notification()
        claim_due_notifications("worker-a", lease_seconds=1)
        connection = get_db_connection()
        expired = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        connection.execute("UPDATE notifications SET lease_expires_at = ?", (expired,))
        connection.commit(); connection.close()
        reclaimed = claim_due_notifications("worker-b")
        self.assertEqual(len(reclaimed), 1)
        self.assertEqual(reclaimed[0]["claimed_by"], "worker-b")
        self.assertEqual(reclaimed[0]["attempt_count"], 2)

    def test_worker_marks_success_and_persists_provider_id(self):
        notification_id = self.add_notification()
        adapter = FakeAdapter(); registry = AdapterRegistry(); registry.register(adapter)
        result = NotificationWorker("worker-a", registry).run_once()
        self.assertEqual(result["sent"], 1)
        connection = get_db_connection()
        row = connection.execute("SELECT delivery_status, provider_message_id, claimed_by FROM notifications WHERE notification_id = ?", (notification_id,)).fetchone()
        connection.close()
        self.assertEqual(row, ("sent", "tg-42", None))
        self.assertEqual(len(adapter.calls), 1)

    def test_retryable_result_returns_to_pending_with_retry_time(self):
        self.add_notification()
        adapter = FakeAdapter(DeliveryResult("retryable", error_code="temporary", error_message="Try later."))
        registry = AdapterRegistry(); registry.register(adapter)
        result = NotificationWorker("worker-a", registry, retry_delay_seconds=0).run_once()
        self.assertEqual(result["retry_scheduled"], 1)
        connection = get_db_connection()
        row = connection.execute("SELECT delivery_status, next_retry_at, claimed_by FROM notifications").fetchone()
        connection.close()
        self.assertEqual(row[0], "pending"); self.assertIsNotNone(row[1]); self.assertIsNone(row[2])

    def test_permanent_failure_marks_failed(self):
        self.add_notification()
        adapter = FakeAdapter(DeliveryResult("failed", error_code="bad_request", error_message="Rejected."))
        registry = AdapterRegistry(); registry.register(adapter)
        result = NotificationWorker("worker-a", registry).run_once()
        self.assertEqual(result["failed"], 1)
        connection = get_db_connection(); self.assertEqual(connection.execute("SELECT delivery_status FROM notifications").fetchone()[0], "failed"); connection.close()

    def test_missing_adapter_fails_closed(self):
        self.add_notification(channel="email", recipient="person@example.com")
        result = NotificationWorker("worker-a", AdapterRegistry()).run_once()
        self.assertEqual(result["adapter_missing"], 1)
        connection = get_db_connection(); self.assertEqual(connection.execute("SELECT delivery_status FROM notifications").fetchone()[0], "failed"); connection.close()

    def test_worker_does_not_persist_arbitrary_adapter_exception_text(self):
        class ExplodingAdapter:
            channel = "telegram"
            def deliver(self, notification): raise RuntimeError("secret-token-should-not-leak")
        self.add_notification(); registry = AdapterRegistry(); registry.register(ExplodingAdapter())
        NotificationWorker("worker-a", registry, retry_delay_seconds=0).run_once()
        connection = get_db_connection(); error = connection.execute("SELECT error_message FROM notifications").fetchone()[0]; connection.close()
        self.assertNotIn("secret-token-should-not-leak", error)

    def test_disabled_preference_suppresses_already_queued_notification(self):
        self.add_notification()
        connection = get_db_connection(); connection.execute("UPDATE notification_channel_preferences SET enabled = 0 WHERE user_id = 1 AND channel = 'telegram'"); connection.commit(); connection.close()
        adapter = FakeAdapter(); registry = AdapterRegistry(); registry.register(adapter)
        result = NotificationWorker("worker-a", registry).run_once()
        self.assertEqual(result["suppressed_unauthorized"], 1)
        self.assertEqual(adapter.calls, [])
        connection = get_db_connection(); row = connection.execute("SELECT delivery_status, error_message FROM notifications").fetchone(); connection.close()
        self.assertEqual(row[0], "failed"); self.assertIn("authorization", row[1].lower())

    def test_changed_recipient_suppresses_old_queued_destination(self):
        self.add_notification()
        save_channel_preference(1, "telegram", "987654321", True, datetime.now(timezone.utc).isoformat())
        adapter = FakeAdapter(); registry = AdapterRegistry(); registry.register(adapter)
        result = NotificationWorker("worker-a", registry).run_once()
        self.assertEqual(result["suppressed_unauthorized"], 1); self.assertEqual(adapter.calls, [])

    def test_disabled_shipment_monitoring_suppresses_delivery(self):
        self.add_notification()
        connection = get_db_connection(); connection.execute("UPDATE user_shipments SET monitoring_enabled = 0 WHERE user_shipment_id = 1"); connection.commit(); connection.close()
        adapter = FakeAdapter(); registry = AdapterRegistry(); registry.register(adapter)
        result = NotificationWorker("worker-a", registry).run_once()
        self.assertEqual(result["suppressed_unauthorized"], 1); self.assertEqual(adapter.calls, [])

    def test_worker_rejects_invalid_configuration(self):
        with self.assertRaises(ValueError): NotificationWorker("worker", AdapterRegistry(), max_attempts=0)


    def test_simultaneous_workers_cannot_claim_same_notification(self):
        self.add_notification()
        barrier = Barrier(2)

        def claim(worker_id):
            barrier.wait(timeout=5)
            return claim_due_notifications(worker_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(claim, "worker-concurrent-a")
            second = pool.submit(claim, "worker-concurrent-b")
            results = [first.result(timeout=10), second.result(timeout=10)]

        claimed = [item for batch in results for item in batch]
        self.assertEqual(len(claimed), 1)
        self.assertIn(
            claimed[0]["claimed_by"],
            {"worker-concurrent-a", "worker-concurrent-b"},
        )


if __name__ == "__main__": unittest.main()
