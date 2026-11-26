import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from database import monitoring_job_repository as repo


class MonitoringJobRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "monitoring_test.db"
        self.env = patch.dict(os.environ, {"INDIAPOST_DB_PATH": str(self.db_path)})
        self.env.start()
        self._create_schema()

    def tearDown(self):
        self.env.stop()
        self.temp_dir.cleanup()

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _create_schema(self):
        con = self._connect()
        try:
            con.executescript("""
                CREATE TABLE shipments (
                    shipment_id INTEGER PRIMARY KEY,
                    current_status TEXT
                );
                CREATE TABLE user_shipments (
                    user_shipment_id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    shipment_id INTEGER NOT NULL,
                    monitoring_enabled INTEGER NOT NULL,
                    monitoring_interval_hours INTEGER NOT NULL,
                    next_check_at TEXT,
                    monitoring_completed_at TEXT,
                    FOREIGN KEY (shipment_id) REFERENCES shipments(shipment_id)
                );
                CREATE TABLE monitoring_jobs (
                    monitoring_job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    shipment_id INTEGER NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'scheduled'
                        CHECK (status IN ('scheduled','processing','retryable','completed','disabled')),
                    next_run_at TEXT,
                    claimed_by TEXT,
                    claimed_at TEXT,
                    lease_expires_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at TEXT,
                    last_success_at TEXT,
                    next_retry_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (shipment_id) REFERENCES shipments(shipment_id)
                );
                CREATE INDEX idx_monitoring_jobs_due
                    ON monitoring_jobs(status, next_run_at);
                CREATE INDEX idx_monitoring_jobs_lease
                    ON monitoring_jobs(status, lease_expires_at);
            """)
            con.commit()
        finally:
            con.close()

    def _seed(self, shipment_id=1, status="In Transit", subscriptions=None):
        con = self._connect()
        try:
            con.execute(
                "INSERT INTO shipments(shipment_id,current_status) VALUES (?,?)",
                (shipment_id, status),
            )
            for index, item in enumerate(subscriptions or [], start=1):
                enabled, next_check = item
                con.execute("""
                    INSERT INTO user_shipments
                        (user_id, shipment_id, monitoring_enabled,
                         monitoring_interval_hours, next_check_at,
                         monitoring_completed_at)
                    VALUES (?, ?, ?, 6, ?, NULL)
                """, (index, shipment_id, enabled, next_check))
            con.commit()
        finally:
            con.close()

    def test_sync_creates_one_job_for_active_shipment(self):
        self._seed(subscriptions=[(1, "2026-11-16T10:00:00+00:00")])
        job = repo.sync_monitoring_job(1, now="2026-11-16T09:00:00+00:00")
        self.assertEqual(job["status"], "scheduled")
        self.assertEqual(job["next_run_at"], "2026-11-16T10:00:00+00:00")

    def test_sync_uses_earliest_timestamp_by_instant(self):
        self._seed(subscriptions=[
            (1, "2026-11-16T10:00:00+05:30"),
            (1, "2026-11-16T05:00:00+00:00"),
        ])
        job = repo.sync_monitoring_job(1, now="2026-11-16T00:00:00+00:00")
        self.assertEqual(job["next_run_at"], "2026-11-16T04:30:00+00:00")

    def test_sync_disables_job_when_no_active_subscription(self):
        self._seed(subscriptions=[(0, None)])
        job = repo.sync_monitoring_job(1, now="2026-11-16T00:00:00+00:00")
        # No job is needed when a shipment has never had an active subscription.
        self.assertIsNone(job)

    def test_sync_marks_delivered_shipment_completed(self):
        self._seed(status="Delivered", subscriptions=[(1, None)])
        job = repo.sync_monitoring_job(1, now="2026-11-16T00:00:00+00:00")
        self.assertEqual(job["status"], "completed")
        self.assertIsNone(job["next_run_at"])

    def test_null_next_check_is_due_now(self):
        self._seed(subscriptions=[(1, None)])
        job = repo.sync_monitoring_job(1, now="2026-11-16T12:00:00+00:00")
        self.assertEqual(job["next_run_at"], "2026-11-16T12:00:00+00:00")

    def test_due_list_does_not_claim(self):
        self._seed(subscriptions=[(1, "2026-11-16T10:00:00+00:00")])
        repo.sync_monitoring_job(1, now="2026-11-16T09:00:00+00:00")
        due = repo.list_due_monitoring_jobs(now="2026-11-16T10:00:00+00:00")
        self.assertEqual(len(due), 1)
        self.assertEqual(repo.get_monitoring_job(1)["status"], "scheduled")

    def test_claim_increments_attempt_and_sets_lease(self):
        self._seed(subscriptions=[(1, "2026-11-16T10:00:00+00:00")])
        repo.sync_monitoring_job(1, now="2026-11-16T09:00:00+00:00")
        claimed = repo.claim_due_monitoring_jobs(
            "worker-A", now="2026-11-16T10:00:00+00:00", lease_seconds=60
        )
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["status"], "processing")
        self.assertEqual(claimed[0]["claimed_by"], "worker-A")
        self.assertEqual(claimed[0]["attempt_count"], 1)
        self.assertEqual(
            claimed[0]["lease_expires_at"], "2026-11-16T10:01:00+00:00"
        )

    def test_second_worker_cannot_claim_already_claimed_job(self):
        self._seed(subscriptions=[(1, "2026-11-16T10:00:00+00:00")])
        repo.sync_monitoring_job(1, now="2026-11-16T09:00:00+00:00")
        first = repo.claim_due_monitoring_jobs(
            "worker-A", now="2026-11-16T10:00:00+00:00"
        )
        second = repo.claim_due_monitoring_jobs(
            "worker-B", now="2026-11-16T10:00:01+00:00"
        )
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_concurrent_workers_claim_distinct_jobs(self):
        for shipment_id in (1, 2):
            self._seed(shipment_id=shipment_id, subscriptions=[
                (1, "2026-11-16T10:00:00+00:00")
            ])
            repo.sync_monitoring_job(
                shipment_id, now="2026-11-16T09:00:00+00:00"
            )

        barrier = threading.Barrier(2)
        results = []
        errors = []

        def run(worker):
            try:
                barrier.wait()
                results.append(repo.claim_due_monitoring_jobs(
                    worker, limit=1, now="2026-11-16T10:00:00+00:00"
                ))
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=run, args=("worker-A",)),
            threading.Thread(target=run, args=("worker-B",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        claimed_ids = [
            item["monitoring_job_id"]
            for batch in results for item in batch
        ]
        self.assertEqual(len(claimed_ids), 2)
        self.assertEqual(len(set(claimed_ids)), 2)

    def test_lease_renewal_requires_current_owner(self):
        self._seed(subscriptions=[(1, "2026-11-16T10:00:00+00:00")])
        repo.sync_monitoring_job(1, now="2026-11-16T09:00:00+00:00")
        claimed = repo.claim_due_monitoring_jobs(
            "worker-A", now="2026-11-16T10:00:00+00:00", lease_seconds=60
        )[0]
        self.assertTrue(repo.renew_monitoring_job_lease(
            claimed["monitoring_job_id"], "worker-A",
            now="2026-11-16T10:00:30+00:00", lease_seconds=120
        ))
        self.assertFalse(repo.renew_monitoring_job_lease(
            claimed["monitoring_job_id"], "worker-B",
            now="2026-11-16T10:00:31+00:00", lease_seconds=120
        ))

    def test_expired_lease_recovery_moves_job_to_retryable(self):
        self._seed(subscriptions=[(1, "2026-11-16T10:00:00+00:00")])
        repo.sync_monitoring_job(1, now="2026-11-16T09:00:00+00:00")
        repo.claim_due_monitoring_jobs(
            "worker-A", now="2026-11-16T10:00:00+00:00", lease_seconds=30
        )
        count = repo.recover_expired_monitoring_job_leases(
            now="2026-11-16T10:00:30+00:00"
        )
        self.assertEqual(count, 1)
        job = repo.get_monitoring_job(1)
        self.assertEqual(job["status"], "retryable")
        self.assertEqual(job["next_retry_at"], "2026-11-16T10:00:30+00:00")
        self.assertIsNone(job["claimed_by"])

    def test_stale_worker_cannot_complete_after_lease_expiry(self):
        self._seed(subscriptions=[(1, "2026-11-16T10:00:00+00:00")])
        repo.sync_monitoring_job(1, now="2026-11-16T09:00:00+00:00")
        claimed = repo.claim_due_monitoring_jobs(
            "worker-A", now="2026-11-16T10:00:00+00:00", lease_seconds=30
        )[0]
        self.assertFalse(repo.complete_monitoring_job(
            claimed["monitoring_job_id"], "worker-A",
            next_run_at="2026-11-16T12:00:00+00:00",
            now="2026-11-16T10:00:30+00:00"
        ))

    def test_retry_requires_current_owner_and_sets_retry_time(self):
        self._seed(subscriptions=[(1, "2026-11-16T10:00:00+00:00")])
        repo.sync_monitoring_job(1, now="2026-11-16T09:00:00+00:00")
        claimed = repo.claim_due_monitoring_jobs(
            "worker-A", now="2026-11-16T10:00:00+00:00"
        )[0]
        self.assertTrue(repo.schedule_monitoring_job_retry(
            claimed["monitoring_job_id"], "worker-A",
            retry_at="2026-11-16T10:05:00+00:00",
            error="Temporary provider failure",
            now="2026-11-16T10:01:00+00:00"
        ))
        job = repo.get_monitoring_job(1)
        self.assertEqual(job["status"], "retryable")
        self.assertEqual(job["next_retry_at"], "2026-11-16T10:05:00+00:00")

    def test_successful_completion_clears_claim_and_schedules_next_run(self):
        self._seed(subscriptions=[(1, "2026-11-16T10:00:00+00:00")])
        repo.sync_monitoring_job(1, now="2026-11-16T09:00:00+00:00")
        claimed = repo.claim_due_monitoring_jobs(
            "worker-A", now="2026-11-16T10:00:00+00:00"
        )[0]
        self.assertTrue(repo.complete_monitoring_job(
            claimed["monitoring_job_id"], "worker-A",
            next_run_at="2026-11-16T16:00:00+00:00",
            now="2026-11-16T10:01:00+00:00"
        ))
        job = repo.get_monitoring_job(1)
        self.assertEqual(job["status"], "scheduled")
        self.assertEqual(job["next_run_at"], "2026-11-16T16:00:00+00:00")
        self.assertIsNone(job["claimed_by"])

    def test_delivered_completion_stops_future_monitoring(self):
        self._seed(subscriptions=[(1, "2026-11-16T10:00:00+00:00")])
        repo.sync_monitoring_job(1, now="2026-11-16T09:00:00+00:00")
        claimed = repo.claim_due_monitoring_jobs(
            "worker-A", now="2026-11-16T10:00:00+00:00"
        )[0]
        self.assertTrue(repo.complete_monitoring_job(
            claimed["monitoring_job_id"], "worker-A",
            next_run_at=None, delivered=True,
            now="2026-11-16T10:01:00+00:00"
        ))
        job = repo.get_monitoring_job(1)
        self.assertEqual(job["status"], "completed")
        self.assertIsNone(job["next_run_at"])

    def test_sync_does_not_steal_processing_job(self):
        self._seed(subscriptions=[(1, "2026-11-16T10:00:00+00:00")])
        repo.sync_monitoring_job(1, now="2026-11-16T09:00:00+00:00")
        repo.claim_due_monitoring_jobs(
            "worker-A", now="2026-11-16T10:00:00+00:00"
        )
        repo.sync_monitoring_job(1, now="2026-11-16T10:00:01+00:00")
        job = repo.get_monitoring_job(1)
        self.assertEqual(job["status"], "processing")
        self.assertEqual(job["claimed_by"], "worker-A")

    def test_invalid_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            repo.claim_due_monitoring_jobs("")
        with self.assertRaises(ValueError):
            repo.list_due_monitoring_jobs(limit=0)
        with self.assertRaises(ValueError):
            repo.sync_monitoring_job(0)


if __name__ == "__main__":
    unittest.main()
