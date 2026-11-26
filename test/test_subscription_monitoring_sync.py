import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from database.user_shipment_repository import save_user_shipment, get_user_shipment
from database.monitoring_job_repository import get_monitoring_job


class SubscriptionMonitoringSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "sync_test.db"
        self.env = patch.dict(os.environ, {"INDIAPOST_DB_PATH": str(self.db_path)})
        self.env.start()
        con = sqlite3.connect(self.db_path)
        con.executescript("""
            CREATE TABLE shipments (
                shipment_id INTEGER PRIMARY KEY,
                current_status TEXT
            );
            CREATE TABLE user_shipments (
                user_shipment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                shipment_id INTEGER NOT NULL,
                monitoring_enabled INTEGER NOT NULL,
                monitoring_interval_hours INTEGER NOT NULL,
                next_check_at TEXT,
                monitoring_completed_at TEXT,
                created_at TEXT,
                UNIQUE(user_id, shipment_id),
                FOREIGN KEY(shipment_id) REFERENCES shipments(shipment_id)
            );
            CREATE TABLE monitoring_jobs (
                monitoring_job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                shipment_id INTEGER NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'scheduled'
                    CHECK(status IN ('scheduled','processing','retryable','completed','disabled')),
                next_run_at TEXT, claimed_by TEXT, claimed_at TEXT,
                lease_expires_at TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TEXT, last_success_at TEXT, next_retry_at TEXT,
                last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY(shipment_id) REFERENCES shipments(shipment_id)
            );
        """)
        con.execute("INSERT INTO shipments VALUES (1, 'In Transit')")
        con.commit()
        con.close()

    def tearDown(self):
        self.env.stop()
        self.temp_dir.cleanup()

    def test_new_enabled_subscription_creates_job(self):
        save_user_shipment(10, 1, 2, 1)
        job = get_monitoring_job(1)
        self.assertEqual(job["status"], "scheduled")
        self.assertIsNotNone(job["next_run_at"])

    def test_interval_change_resynchronises_job(self):
        save_user_shipment(10, 1, 2, 1)
        save_user_shipment(10, 1, 6, 1)
        sub = get_user_shipment(10, 1)
        job = get_monitoring_job(1)
        from datetime import datetime, timezone
        expected = datetime.fromisoformat(sub["next_check_at"])
        if expected.tzinfo is None:
            expected = expected.replace(tzinfo=timezone.utc)
        expected = expected.astimezone(timezone.utc).isoformat(timespec="seconds")
        self.assertEqual(job["next_run_at"], expected)

    def test_disabling_last_subscription_disables_job(self):
        save_user_shipment(10, 1, 2, 1)
        save_user_shipment(10, 1, 2, 0)
        job = get_monitoring_job(1)
        self.assertEqual(job["status"], "disabled")
        self.assertIsNone(job["next_run_at"])

    def test_other_active_subscriber_keeps_job_scheduled(self):
        save_user_shipment(10, 1, 8, 1)
        save_user_shipment(11, 1, 3, 1)
        save_user_shipment(11, 1, 3, 0)
        self.assertEqual(get_monitoring_job(1)["status"], "scheduled")
        self.assertEqual(get_user_shipment(10, 1)["monitoring_enabled"], 1)

    def test_processing_job_claim_is_not_stolen(self):
        save_user_shipment(10, 1, 2, 1)
        con = sqlite3.connect(self.db_path)
        con.execute("""UPDATE monitoring_jobs SET status='processing',
            claimed_by='worker-A', claimed_at='2026-11-16T10:00:00+00:00',
            lease_expires_at='2026-11-16T10:05:00+00:00' WHERE shipment_id=1""")
        con.commit(); con.close()
        save_user_shipment(10, 1, 6, 1)
        job = get_monitoring_job(1)
        self.assertEqual(job["status"], "processing")
        self.assertEqual(job["claimed_by"], "worker-A")

    def test_job_sync_failure_rolls_back_subscription(self):
        con = sqlite3.connect(self.db_path)
        con.execute("""CREATE TRIGGER fail_job_insert BEFORE INSERT ON monitoring_jobs
            BEGIN SELECT RAISE(ABORT, 'simulated monitoring job failure'); END;""")
        con.commit(); con.close()
        with self.assertRaises(sqlite3.IntegrityError):
            save_user_shipment(10, 1, 2, 1)
        self.assertIsNone(get_user_shipment(10, 1))
        self.assertIsNone(get_monitoring_job(1))


if __name__ == "__main__":
    unittest.main()
