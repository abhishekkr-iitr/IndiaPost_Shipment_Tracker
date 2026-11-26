"""Mocked tests for durable per-attempt notification delivery history."""
import httpx

from database.db_connection import get_db_connection
from notifications.worker import NotificationWorker
try:
    from tests.test_telegram_worker_integration import TelegramWorkerIntegrationTests
except ImportError:
    try:
        from test.test_telegram_worker_integration import TelegramWorkerIntegrationTests
    except ImportError:
        from test_telegram_worker_integration import TelegramWorkerIntegrationTests


class DeliveryAttemptHistoryTests(TelegramWorkerIntegrationTests):
    def _attempt_rows(self, notification_id):
        connection = get_db_connection()
        try:
            return connection.execute(
                """SELECT attempt_number, worker_id, channel, provider, outcome,
                          provider_message_id, error_message, completed_at
                   FROM notification_delivery_attempts
                   WHERE notification_id = ? ORDER BY attempt_number""",
                (notification_id,),
            ).fetchall()
        finally:
            connection.close()

    def test_successful_delivery_records_provider_message_id(self):
        notification_id = self._add_pending_notification()
        result, _ = self._run_pipeline(
            lambda request: httpx.Response(200, json={"ok": True, "result": {"message_id": 781}})
        )
        self.assertEqual(result["sent"], 1)
        rows = self._attempt_rows(notification_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0:5], (1, "telegram-integration-worker", "telegram", "telegram", "sent"))
        self.assertEqual(rows[0][5], "781")
        self.assertIsNotNone(rows[0][7])

    def test_retry_then_success_preserves_both_attempts(self):
        notification_id = self._add_pending_notification()
        first, _ = self._run_pipeline(
            lambda request: httpx.Response(503, json={"ok": False, "error_code": 503})
        )
        self.assertEqual(first["retry_scheduled"], 1)
        second, _ = self._run_pipeline(
            lambda request: httpx.Response(200, json={"ok": True, "result": {"message_id": 782}})
        )
        self.assertEqual(second["sent"], 1)
        rows = self._attempt_rows(notification_id)
        self.assertEqual([row[0] for row in rows], [1, 2])
        self.assertEqual([row[4] for row in rows], ["retry_scheduled", "sent"])
        self.assertEqual(rows[1][5], "782")

    def test_permanent_failure_is_recorded(self):
        notification_id = self._add_pending_notification()
        result, _ = self._run_pipeline(
            lambda request: httpx.Response(400, json={"ok": False, "error_code": 400})
        )
        self.assertEqual(result["failed"], 1)
        rows = self._attempt_rows(notification_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][4], "failed")
        self.assertIsNotNone(rows[0][6])

    def test_expired_lease_closes_previous_attempt_as_interrupted(self):
        self._add_pending_notification()
        first = __import__("database.notification_delivery_repository", fromlist=["claim_due_notifications"]).claim_due_notifications(
            "worker-one", lease_seconds=1
        )
        self.assertEqual(len(first), 1)
        connection = get_db_connection()
        connection.execute(
            "UPDATE notifications SET lease_expires_at = '2000-01-01T00:00:00+00:00'"
        )
        connection.commit()
        connection.close()
        second = __import__("database.notification_delivery_repository", fromlist=["claim_due_notifications"]).claim_due_notifications(
            "worker-two", lease_seconds=60
        )
        self.assertEqual(len(second), 1)
        rows = self._attempt_rows(first[0]["notification_id"])
        self.assertEqual([row[4] for row in rows], ["interrupted", "in_progress"])

    def test_existing_notification_columns_remain_aggregate_and_unchanged(self):
        notification_id = self._add_pending_notification()
        self._run_pipeline(
            lambda request: httpx.Response(200, json={"ok": True, "result": {"message_id": 783}})
        )
        connection = get_db_connection()
        try:
            row = connection.execute(
                "SELECT delivery_status, attempt_count, provider_message_id FROM notifications WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row, ("sent", 1, "783"))


if __name__ == "__main__":
    import unittest
    unittest.main()
