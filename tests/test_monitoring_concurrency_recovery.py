"""Additive Step 5.5.3 concurrency and lease-recovery evaluation tests.

All database tests use an isolated temporary SQLite database. No provider calls
or live project database access are performed.
"""
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from database import monitoring_job_repository as repo
from monitoring import job_outcomes
from monitoring.monitoring_worker import _LeaseHeartbeat


NOW = "2026-11-16T10:00:00+00:00"


class MonitoringConcurrencyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "isolated_monitoring.db"
        self.env_patch = patch.dict(os.environ, {"INDIAPOST_DB_PATH": str(self.db_path)})
        self.env_patch.start()
        con = sqlite3.connect(self.db_path)
        con.executescript("""
            PRAGMA foreign_keys = ON;
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
            CREATE INDEX idx_monitoring_jobs_due ON monitoring_jobs(status, next_run_at);
            CREATE INDEX idx_monitoring_jobs_lease ON monitoring_jobs(status, lease_expires_at);
        """)
        con.commit()
        con.close()

    def tearDown(self):
        self.env_patch.stop()
        self.temp.cleanup()

    def seed_due_job(self):
        con = sqlite3.connect(self.db_path)
        con.execute("INSERT INTO shipments VALUES (1, 'In Transit')")
        con.execute("""
            INSERT INTO user_shipments
                (user_id, shipment_id, monitoring_enabled,
                 monitoring_interval_hours, next_check_at, monitoring_completed_at)
            VALUES (1, 1, 1, 6, ?, NULL)
        """, (NOW,))
        con.commit()
        con.close()
        return repo.sync_monitoring_job(1, now=NOW)["monitoring_job_id"]

    def read_job(self, job_id):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                "SELECT * FROM monitoring_jobs WHERE monitoring_job_id=?", (job_id,)
            ).fetchone()
            return dict(row)
        finally:
            con.close()

    def test_two_workers_racing_for_one_job_produce_exactly_one_claim(self):
        job_id = self.seed_due_job()
        barrier = threading.Barrier(2)
        results, errors = [], []

        def claim(worker):
            try:
                barrier.wait(timeout=5)
                results.append(repo.claim_due_monitoring_jobs(
                    worker, limit=1, lease_seconds=60, now=NOW
                ))
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=claim, args=("worker-A",)),
            threading.Thread(target=claim, args=("worker-B",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads), "claim thread hung")
        self.assertEqual(errors, [])
        claimed = [item for batch in results for item in batch]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["monitoring_job_id"], job_id)
        self.assertIn(claimed[0]["claimed_by"], {"worker-A", "worker-B"})

    def test_expired_claim_is_recovered_and_second_worker_can_claim(self):
        job_id = self.seed_due_job()
        first = repo.claim_due_monitoring_jobs(
            "worker-A", now=NOW, lease_seconds=30
        )
        self.assertEqual(len(first), 1)

        recovered_at = "2026-11-16T10:00:30+00:00"
        self.assertEqual(
            repo.recover_expired_monitoring_job_leases(now=recovered_at), 1
        )
        recovered = self.read_job(job_id)
        self.assertEqual(recovered["status"], "retryable")
        self.assertIsNone(recovered["claimed_by"])

        second = repo.claim_due_monitoring_jobs(
            "worker-B", now=recovered_at, lease_seconds=60
        )
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["claimed_by"], "worker-B")
        self.assertEqual(second[0]["attempt_count"], 2)

    def test_stale_worker_cannot_record_failure_after_recovery_and_reclaim(self):
        job_id = self.seed_due_job()
        repo.claim_due_monitoring_jobs("worker-A", now=NOW, lease_seconds=30)
        recovered_at = "2026-11-16T10:00:30+00:00"
        repo.recover_expired_monitoring_job_leases(now=recovered_at)
        repo.claim_due_monitoring_jobs("worker-B", now=recovered_at, lease_seconds=60)

        stale = job_outcomes.record_monitoring_failure(
            job_id, "worker-A", error="late failure",
            now="2026-11-16T10:00:31+00:00",
        )
        self.assertEqual(stale, {"recorded": False, "reason": "lease_lost"})
        current = self.read_job(job_id)
        self.assertEqual(current["claimed_by"], "worker-B")
        self.assertEqual(current["status"], "processing")
        # Lease recovery intentionally records this diagnostic; the stale
        # worker must not overwrite it with its late failure message.
        self.assertEqual(current["last_error"], "Execution lease expired.")

    def test_stale_worker_cannot_complete_after_recovery_and_reclaim(self):
        job_id = self.seed_due_job()
        repo.claim_due_monitoring_jobs("worker-A", now=NOW, lease_seconds=30)
        recovered_at = "2026-11-16T10:00:30+00:00"
        repo.recover_expired_monitoring_job_leases(now=recovered_at)
        repo.claim_due_monitoring_jobs("worker-B", now=recovered_at, lease_seconds=60)

        stale = job_outcomes.complete_monitoring_cycle(
            job_id, "worker-A", shipment_status="Delivered",
            now="2026-11-16T10:00:31+00:00",
        )
        self.assertEqual(stale, {"completed": False, "reason": "lease_lost"})
        current = self.read_job(job_id)
        self.assertEqual(current["claimed_by"], "worker-B")
        self.assertEqual(current["status"], "processing")

        con = sqlite3.connect(self.db_path)
        try:
            subscription = con.execute(
                "SELECT monitoring_completed_at FROM user_shipments WHERE shipment_id=1"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertIsNone(subscription, "stale Delivered result must not complete subscriptions")

    def test_heartbeat_renews_claimed_jobs_during_long_processing(self):
        job = {"monitoring_job_id": 77}
        with patch(
            "monitoring.monitoring_worker.renew_monitoring_job_lease",
            return_value=True,
        ) as renew:
            heartbeat = _LeaseHeartbeat([job], "worker-A", lease_seconds=1)
            heartbeat.start()
            time.sleep(0.45)
            heartbeat.stop()

        self.assertGreaterEqual(renew.call_count, 1)
        renew.assert_any_call(77, "worker-A", lease_seconds=1)

    def test_heartbeat_marks_job_lost_when_renewal_is_rejected(self):
        job = {"monitoring_job_id": 78}
        with patch(
            "monitoring.monitoring_worker.renew_monitoring_job_lease",
            return_value=False,
        ):
            heartbeat = _LeaseHeartbeat([job], "worker-A", lease_seconds=1)
            heartbeat.start()
            time.sleep(0.45)
            heartbeat.stop()

        self.assertIn(78, heartbeat.lost)


if __name__ == "__main__":
    unittest.main()
