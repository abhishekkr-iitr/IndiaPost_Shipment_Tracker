"""Tests for operational tooling: database backup, health diagnostics, and orchestrator."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from database import schema
from database.migrations import apply_migrations
from orchestrator import ServiceOrchestrator
from scripts.backup_database import perform_backup, prune_old_backups, verify_restore_target
from scripts.health_inspector import (
    _mask_secret,
    inspect_channel_readiness,
    inspect_database,
    inspect_environment,
    run_full_diagnostics,
)


class OperationalToolingTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_ops.db"
        self.backup_dir = Path(self.temp_dir.name) / "backups"
        self.prev_env = os.environ.get("INDIAPOST_DB_PATH")
        os.environ["INDIAPOST_DB_PATH"] = str(self.db_path)

        conn = sqlite3.connect(str(self.db_path))
        try:
            schema.create_users_table(connection=conn)
            schema.create_shipments_table(connection=conn)
            schema.create_user_shipments_table(connection=conn)
            schema.create_checkpoints_table(connection=conn)
            schema.create_tracking_checks_table(connection=conn)
            schema.create_notification_preferences_table(connection=conn)
            schema.create_notifications_table(connection=conn)
            apply_migrations(conn)
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        if self.prev_env is not None:
            os.environ["INDIAPOST_DB_PATH"] = self.prev_env
        else:
            os.environ.pop("INDIAPOST_DB_PATH", None)
        self.temp_dir.cleanup()

    # -----------------------------------------------------------------------
    # Database Backup & Retention Tests
    # -----------------------------------------------------------------------

    def test_perform_backup_and_verify(self):
        result = perform_backup(destination_dir=self.backup_dir, keep_count=5)
        self.assertTrue(result["success"])
        self.assertEqual(result["integrity"], "ok")
        self.assertEqual(result["foreign_key_violations"], 0)
        self.assertTrue(Path(result["backup_db"]).exists())

        # Verify target backup file
        verify_res = verify_restore_target(Path(result["backup_db"]))
        self.assertTrue(verify_res["verified"])
        self.assertEqual(verify_res["integrity"], "ok")
        self.assertIn("notifications", verify_res["tables"])

    def test_prune_old_backups(self):
        # Create 5 synthetic backup directories
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        for i in range(1, 6):
            d = self.backup_dir / f"shipments_backup_20261120_10000{i}"
            d.mkdir(parents=True, exist_ok=True)

        pruned = prune_old_backups(self.backup_dir, keep_count=2)
        self.assertEqual(len(pruned), 3)
        self.assertIn("shipments_backup_20261120_100001", pruned)
        self.assertIn("shipments_backup_20261120_100002", pruned)
        self.assertIn("shipments_backup_20261120_100003", pruned)

        # Remaining directories should be exactly 2
        remaining = list(self.backup_dir.iterdir())
        self.assertEqual(len(remaining), 2)

    # -----------------------------------------------------------------------
    # Health Diagnostics Inspector Tests
    # -----------------------------------------------------------------------

    def test_secret_masking(self):
        self.assertEqual(_mask_secret(None), "[NOT CONFIGURED]")
        self.assertEqual(_mask_secret(""), "[NOT CONFIGURED]")
        self.assertEqual(_mask_secret("123"), "[CONFIGURED (len=3)]")
        masked = _mask_secret("sk-1234567890abcdef", 3)
        self.assertTrue(masked.startswith("[CONFIGURED: sk-..."))
        self.assertNotIn("abcdef", masked)

    def test_environment_inspection(self):
        env = inspect_environment()
        self.assertIn("openai_api_key", env)
        self.assertIn("telegram_bot_token", env)
        self.assertIn("log_level", env)

    def test_database_and_full_diagnostics(self):
        db_info = inspect_database()
        self.assertEqual(db_info["integrity"], "ok")
        self.assertEqual(db_info["foreign_key_violations"], 0)
        self.assertGreaterEqual(db_info["schema_migration_version"], 11)

        diag = run_full_diagnostics()
        self.assertTrue(diag["healthy"])
        self.assertIn("database", diag)
        self.assertIn("channels", diag)

    # -----------------------------------------------------------------------
    # Orchestrator Configuration Tests
    # -----------------------------------------------------------------------

    def test_orchestrator_build_services(self):
        orchestrator = ServiceOrchestrator(
            host="0.0.0.0",
            port=9000,
            enable_monitoring=True,
            enable_notifications=True,
        )
        services = orchestrator.build_services()
        names = [s.name for s in services]
        self.assertIn("web-server", names)
        self.assertIn("monitoring-scheduler", names)
        self.assertIn("notification-scheduler", names)

        # Web server args check
        web_svc = next(s for s in services if s.name == "web-server")
        self.assertIn("0.0.0.0", web_svc.cmd)
        self.assertIn("9000", web_svc.cmd)

    def test_orchestrator_skips_disabled_services(self):
        orchestrator = ServiceOrchestrator(
            enable_monitoring=False,
            enable_notifications=False,
        )
        services = orchestrator.build_services()
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0].name, "web-server")


if __name__ == "__main__":
    unittest.main()
