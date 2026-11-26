import unittest
from datetime import datetime, timedelta, timezone

from database.db_connection import get_db_connection
from notifications.delivery_diagnostics import get_delivery_diagnostics
try:
    from tests.test_notification_worker import WorkerFixture
except ImportError:
    try:
        from test.test_notification_worker import WorkerFixture
    except ImportError:
        from test_notification_worker import WorkerFixture


class DeliveryDiagnosticsTests(WorkerFixture):
    def test_empty_database_returns_zero_counts(self):
        report = get_delivery_diagnostics()
        self.assertEqual(report["total_notifications"], 0)
        self.assertEqual(report["by_status"], {"pending": 0, "sent": 0, "failed": 0, "other": 0})
        self.assertEqual(report["expired_worker_leases"], 0)

    def test_reports_status_retry_and_attempt_counts(self):
        pending_id = self.add_notification("pending")
        sent_id = self.add_notification("sent")
        failed_id = self.add_notification("failed")
        now = datetime.now(timezone.utc)
        due = (now - timedelta(minutes=2)).isoformat()
        waiting = (now + timedelta(minutes=2)).isoformat()

        connection = get_db_connection()
        connection.execute(
            "UPDATE notifications SET next_retry_at = ?, attempt_count = 4 WHERE notification_id = ?",
            (due, pending_id),
        )
        connection.execute(
            "UPDATE notifications SET delivery_status = 'sent', next_retry_at = NULL WHERE notification_id = ?",
            (sent_id,),
        )
        connection.execute(
            "UPDATE notifications SET delivery_status = 'failed', next_retry_at = NULL WHERE notification_id = ?",
            (failed_id,),
        )
        connection.commit()
        connection.close()

        report = get_delivery_diagnostics(max_attempts=5, now=now)
        self.assertEqual(report["total_notifications"], 3)
        self.assertEqual(report["by_status"], {"pending": 1, "sent": 1, "failed": 1, "other": 0})
        self.assertEqual(report["pending_retry_due"], 1)
        self.assertEqual(report["pending_at_or_near_attempt_limit"], 1)

    def test_reports_expired_and_active_leases(self):
        expired_id = self.add_notification("expired")
        active_id = self.add_notification("active")
        now = datetime.now(timezone.utc)
        connection = get_db_connection()
        connection.execute(
            """UPDATE notifications
               SET claimed_by = 'crashed-worker', claimed_at = ?, lease_expires_at = ?
               WHERE notification_id = ?""",
            ((now - timedelta(minutes=5)).isoformat(),
             (now - timedelta(minutes=1)).isoformat(), expired_id),
        )
        connection.execute(
            """UPDATE notifications
               SET claimed_by = 'live-worker', claimed_at = ?, lease_expires_at = ?
               WHERE notification_id = ?""",
            (now.isoformat(), (now + timedelta(minutes=2)).isoformat(), active_id),
        )
        connection.commit()
        connection.close()

        report = get_delivery_diagnostics(now=now)
        self.assertEqual(report["expired_worker_leases"], 1)
        self.assertEqual(report["active_worker_leases"], 1)

    def test_diagnostics_are_read_only(self):
        notification_id = self.add_notification()
        before = get_db_connection()
        original = before.execute(
            "SELECT delivery_status, attempt_count, claimed_by FROM notifications WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()
        before.close()

        get_delivery_diagnostics()

        after = get_db_connection()
        current = after.execute(
            "SELECT delivery_status, attempt_count, claimed_by FROM notifications WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()
        after.close()
        self.assertEqual(current, original)

    def test_rejects_invalid_attempt_limit_and_naive_time(self):
        with self.assertRaises(ValueError):
            get_delivery_diagnostics(max_attempts=0)
        with self.assertRaises(ValueError):
            get_delivery_diagnostics(now=datetime(2026, 1, 1))


if __name__ == "__main__":
    unittest.main()
