import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


class NotificationChannelPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "stage9_step3.db")
        self.env = patch.dict(os.environ, {"INDIAPOST_DB_PATH": self.db_path})
        self.env.start()

        from database.schema import create_users_table, create_notification_preferences_table
        from database.db_connection import get_db_connection
        from database.migrations import apply_migrations

        create_users_table()
        create_notification_preferences_table()
        connection = get_db_connection()
        try:
            now = datetime.now(timezone.utc).isoformat()
            connection.execute(
                "INSERT INTO users (external_user_id, created_at) VALUES (?, ?)",
                ("stage9-test-user", now),
            )
            self.user_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
            connection.execute(
                """INSERT INTO notification_preferences
                   (user_id, email, mobile_number, email_enabled, sms_enabled,
                    whatsapp_enabled, created_at, updated_at)
                   VALUES (?, ?, ?, 1, 1, 0, ?, ?)""",
                (self.user_id, "legacy@example.com", "+919876543210", now, now),
            )
            connection.commit()
        finally:
            connection.close()
        self.migration_result = apply_migrations()

        from database.notification_channel_preference_repository import (
            save_channel_preference, get_channel_preference,
            list_channel_preferences, get_eligible_recipients,
        )
        self.save = save_channel_preference
        self.get = get_channel_preference
        self.list = list_channel_preferences
        self.eligible = get_eligible_recipients

    def tearDown(self):
        self.env.stop()
        self.temp_dir.cleanup()

    def test_migration_applies_version_four(self):
        self.assertEqual(self.migration_result["current_version"], 4)
        self.assertIn(4, self.migration_result["applied"])

    def test_migration_backfills_legacy_channels_without_falsely_verifying(self):
        email = self.get(self.user_id, "email")
        sms = self.get(self.user_id, "sms")
        whatsapp = self.get(self.user_id, "whatsapp")
        self.assertEqual(email["recipient"], "legacy@example.com")
        self.assertEqual(email["enabled"], 1)
        self.assertIsNone(email["verified_at"])
        self.assertEqual(sms["recipient"], "+919876543210")
        self.assertEqual(sms["enabled"], 1)
        self.assertEqual(whatsapp["enabled"], 0)
        self.assertEqual(self.eligible(self.user_id), [])

    def test_save_and_retrieve_verified_telegram_preference(self):
        verified = datetime.now(timezone.utc).isoformat()
        self.assertTrue(self.save(self.user_id, "telegram", "123456789", True, verified))
        row = self.get(self.user_id, "telegram")
        self.assertEqual(row["recipient"], "123456789")
        self.assertEqual(row["enabled"], 1)
        self.assertIsNotNone(row["verified_at"])

    def test_enabled_preference_requires_recipient_and_verification(self):
        with self.assertRaises(ValueError):
            self.save(self.user_id, "telegram", None, True, None)
        with self.assertRaises(ValueError):
            self.save(self.user_id, "telegram", "123456789", True, None)

    def test_disabled_preference_is_not_eligible(self):
        self.save(self.user_id, "telegram", "123456789", False, None)
        self.assertEqual(self.eligible(self.user_id), [])

    def test_unverified_legacy_channels_are_not_eligible(self):
        self.assertEqual(self.eligible(self.user_id), [])
        self.save(
            self.user_id, "email", "legacy@example.com", True,
            datetime.now(timezone.utc).isoformat(),
        )
        self.assertEqual([r["channel"] for r in self.eligible(self.user_id)], ["email"])

    def test_list_preferences_can_filter_enabled(self):
        self.save(self.user_id, "telegram", "123456789", False, None)
        all_rows = self.list(self.user_id)
        enabled_rows = self.list(self.user_id, enabled_only=True)
        self.assertEqual(len(all_rows), 4)
        self.assertEqual(len(enabled_rows), 2)

    def test_invalid_channel_rejected(self):
        with self.assertRaises(ValueError):
            self.save(self.user_id, "carrier_pigeon", "x", False)

    def test_invalid_recipient_formats_rejected(self):
        with self.assertRaises(ValueError):
            self.save(self.user_id, "email", "not-an-email", False)
        with self.assertRaises(ValueError):
            self.save(self.user_id, "sms", "9876543210", False)
        with self.assertRaises(ValueError):
            self.save(self.user_id, "telegram", "@username", False)

    def test_nonexistent_user_rejected_by_foreign_key(self):
        with self.assertRaises(Exception):
            self.save(999999, "telegram", "123456789", False)

    def test_preference_upsert_updates_one_channel_only(self):
        self.save(self.user_id, "telegram", "123456789", False)
        self.save(self.user_id, "telegram", "987654321", False)
        self.assertEqual(self.get(self.user_id, "telegram")["recipient"], "987654321")
        self.assertEqual(len(self.list(self.user_id)), 4)

    def test_migration_is_idempotent(self):
        from database.migrations import apply_migrations
        result = apply_migrations()
        self.assertTrue(result["already_current"])
        self.assertEqual(result["applied"], [])


if __name__ == "__main__":
    unittest.main()
