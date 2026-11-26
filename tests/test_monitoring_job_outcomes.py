"""Focused tests for Step 5.4.3 job outcome transitions and orchestration."""

import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from monitoring.job_outcomes import (
    complete_monitoring_cycle,
    record_monitoring_failure,
)
from monitoring.monitoring_worker import MonitoringWorker


NOW = "2026-11-16T10:00:00+00:00"


class OutcomeDatabase:
    def __init__(self, attempt_count=1, subscribers=((1, 6), (2, 2))):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("""
            CREATE TABLE monitoring_jobs (
                monitoring_job_id INTEGER PRIMARY KEY,
                shipment_id INTEGER,
                status TEXT,
                next_run_at TEXT,
                next_retry_at TEXT,
                claimed_by TEXT,
                claimed_at TEXT,
                lease_expires_at TEXT,
                attempt_count INTEGER,
                last_success_at TEXT,
                last_error TEXT,
                updated_at TEXT
            )
        """)
        self.connection.execute("""
            CREATE TABLE user_shipments (
                user_shipment_id INTEGER PRIMARY KEY,
                shipment_id INTEGER,
                monitoring_enabled INTEGER,
                monitoring_interval_hours INTEGER,
                next_check_at TEXT,
                monitoring_completed_at TEXT
            )
        """)
        self.connection.execute("""
            INSERT INTO monitoring_jobs VALUES
            (7, 8, 'processing', NULL, NULL, 'worker-A', ?, ?, ?, NULL, NULL, ?)
        """, (NOW, "2026-11-16T11:00:00+00:00", attempt_count, NOW))
        for sid, hours in subscribers:
            self.connection.execute(
                """
                INSERT INTO user_shipments
                (user_shipment_id, shipment_id, monitoring_enabled,
                 monitoring_interval_hours, next_check_at, monitoring_completed_at)
                VALUES (?, 8, 1, ?, NULL, NULL)
                """,
                (sid, hours),
            )
        self.connection.commit()

    def close(self):
        self.connection.close()


class ReusableConnection:
    """Test adapter: production closes each connection; tests reuse one DB."""
    def __init__(self, connection):
        self.connection = connection

    def execute(self, *args, **kwargs):
        return self.connection.execute(*args, **kwargs)

    def commit(self):
        return self.connection.commit()

    def rollback(self):
        return self.connection.rollback()

    def close(self):
        # Keep the in-memory database alive across repository calls.
        return None


class TestMonitoringJobOutcomes(unittest.TestCase):
    def setUp(self):
        self.db = OutcomeDatabase()

    def tearDown(self):
        self.db.close()

    def _connect(self):
        return ReusableConnection(self.db.connection)

    def test_success_schedules_earliest_subscriber_and_resets_attempt_count(self):
        with patch("monitoring.job_outcomes.get_db_connection", self._connect):
            result = complete_monitoring_cycle(
                7, "worker-A", shipment_status="In Transit", now=NOW
            )

        self.assertTrue(result["completed"])
        self.assertEqual(result["status"], "scheduled")
        self.assertEqual(result["next_run_at"], "2026-11-16T12:00:00+00:00")
        self.assertEqual(result["subscriber_count"], 2)
        job = self.db.connection.execute(
            "SELECT status, attempt_count, claimed_by FROM monitoring_jobs WHERE monitoring_job_id=7"
        ).fetchone()
        self.assertEqual(job, ("scheduled", 0, None))
        due = self.db.connection.execute(
            "SELECT user_shipment_id, next_check_at FROM user_shipments ORDER BY user_shipment_id"
        ).fetchall()
        self.assertEqual(due[0][1], "2026-11-16T16:00:00+00:00")
        self.assertEqual(due[1][1], "2026-11-16T12:00:00+00:00")

    def test_delivered_completes_job_and_active_subscriptions(self):
        with patch("monitoring.job_outcomes.get_db_connection", self._connect):
            result = complete_monitoring_cycle(
                7, "worker-A", shipment_status="Delivered", now=NOW
            )

        self.assertTrue(result["completed"])
        self.assertTrue(result["delivered"])
        job = self.db.connection.execute(
            "SELECT status, next_run_at FROM monitoring_jobs WHERE monitoring_job_id=7"
        ).fetchone()
        self.assertEqual(job, ("completed", None))
        subs = self.db.connection.execute(
            "SELECT next_check_at, monitoring_completed_at FROM user_shipments"
        ).fetchall()
        self.assertTrue(all(row[0] is None and row[1] == NOW for row in subs))

    def test_stale_worker_cannot_complete_job(self):
        with patch("monitoring.job_outcomes.get_db_connection", self._connect):
            result = complete_monitoring_cycle(
                7, "different-worker", shipment_status="In Transit", now=NOW
            )
        self.assertFalse(result["completed"])
        self.assertEqual(result["reason"], "lease_lost")

    def test_retry_delay_exponential_backoff(self):
        expected = {
            1: (5, "2026-11-16T10:05:00+00:00"),
            2: (10, "2026-11-16T10:10:00+00:00"),
            3: (20, "2026-11-16T10:20:00+00:00"),
            4: (40, "2026-11-16T10:40:00+00:00"),
        }
        for attempts, (delay, retry_at) in expected.items():
            self.db.connection.execute(
                "UPDATE monitoring_jobs SET attempt_count=?, status='processing', claimed_by='worker-A', lease_expires_at=? WHERE monitoring_job_id=7",
                (attempts, "2026-11-16T11:00:00+00:00"),
            )
            self.db.connection.commit()
            with patch("monitoring.job_outcomes.get_db_connection", self._connect):
                result = record_monitoring_failure(
                    7, "worker-A", error="temporary provider error", now=NOW
                )
            self.assertEqual(result["retry_delay_minutes"], delay)
            self.assertEqual(result["next_retry_at"], retry_at)
            self.db.connection.execute(
                "UPDATE monitoring_jobs SET status='processing', claimed_by='worker-A', lease_expires_at=? WHERE monitoring_job_id=7",
                ("2026-11-16T11:00:00+00:00",),
            )
            self.db.connection.commit()

    def test_fifth_failure_exhausts_retries_without_disabling_job(self):
        self.db.connection.execute(
            "UPDATE monitoring_jobs SET attempt_count=5 WHERE monitoring_job_id=7"
        )
        self.db.connection.commit()
        with patch("monitoring.job_outcomes.get_db_connection", self._connect):
            result = record_monitoring_failure(
                7, "worker-A", error="permanent failure", now=NOW
            )
        self.assertTrue(result["exhausted"])
        row = self.db.connection.execute(
            "SELECT status, next_retry_at, last_error, claimed_by FROM monitoring_jobs WHERE monitoring_job_id=7"
        ).fetchone()
        self.assertEqual(row, ("retryable", None, "permanent failure", None))

    def test_stale_worker_cannot_schedule_retry(self):
        with patch("monitoring.job_outcomes.get_db_connection", self._connect):
            result = record_monitoring_failure(
                7, "other-worker", error="failure", now=NOW
            )
        self.assertFalse(result["recorded"])
        self.assertEqual(result["reason"], "lease_lost")


class TestMonitoringWorkerProcessing(unittest.TestCase):
    @patch("monitoring.monitoring_worker.complete_monitoring_cycle")
    @patch("monitoring.monitoring_worker.execute_shipment_tracking")
    @patch.object(MonitoringWorker, "get_shipment_details")
    @patch.object(MonitoringWorker, "claim_due_jobs")
    def test_successful_tracking_completes_monitoring_cycle(
        self, claim, details, execute, complete
    ):
        job = {"monitoring_job_id": 7, "shipment_id": 8}
        claim.return_value = [job]
        details.return_value = {"shipment_id": 8, "tracking_number": "X"}
        execute.return_value = {
            "success": True,
            "tracking_check_id": 33,
            "tracking_data": {"current_status": "In Transit"},
            "inserted_checkpoints": 2,
        }
        complete.return_value = {"completed": True, "status": "scheduled"}

        result = MonitoringWorker("worker-A").run_and_process_once(now=NOW)

        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["tracking_success"])
        complete.assert_called_once_with(
            7, "worker-A", shipment_status="In Transit", now=NOW
        )

    @patch("monitoring.monitoring_worker.record_monitoring_failure")
    @patch("monitoring.monitoring_worker.execute_shipment_tracking")
    @patch.object(MonitoringWorker, "get_shipment_details")
    @patch.object(MonitoringWorker, "claim_due_jobs")
    def test_unsuccessful_tracking_schedules_retry(
        self, claim, details, execute, record_failure
    ):
        claim.return_value = [{"monitoring_job_id": 7, "shipment_id": 8}]
        details.return_value = {"shipment_id": 8, "tracking_number": "X"}
        execute.return_value = {"success": False, "error": "no provider record"}
        record_failure.return_value = {"recorded": True, "exhausted": False}

        result = MonitoringWorker("worker-A").run_and_process_once(now=NOW)

        self.assertFalse(result[0]["tracking_success"])
        record_failure.assert_called_once()

    @patch("monitoring.monitoring_worker.record_monitoring_failure")
    @patch("monitoring.monitoring_worker.execute_shipment_tracking", side_effect=TimeoutError("timeout"))
    @patch.object(MonitoringWorker, "get_shipment_details")
    @patch.object(MonitoringWorker, "claim_due_jobs")
    def test_unexpected_tracking_exception_is_recorded_as_failure(
        self, claim, details, execute, record_failure
    ):
        claim.return_value = [{"monitoring_job_id": 7, "shipment_id": 8}]
        details.return_value = {"shipment_id": 8, "tracking_number": "X"}
        record_failure.return_value = {"recorded": True}

        result = MonitoringWorker("worker-A").run_and_process_once(now=NOW)

        self.assertFalse(result[0]["tracking_success"])
        self.assertIn("timeout", result[0]["error"])
        record_failure.assert_called_once()


if __name__ == "__main__":
    unittest.main()
