"""
Tests for Migration 11: Registered passwordless auth, guest notification identity,
and common notification outbox ownership extension.
"""
import sqlite3
import unittest
from datetime import datetime, timezone

from database.migrations import (
    apply_migrations,
    LATEST_SCHEMA_VERSION,
    MigrationError,
)
from database import schema


class Migration11OwnershipTests(unittest.TestCase):

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.connection.close()

    def _setup_full_v10_database(self):
        schema.create_users_table(connection=self.connection)
        schema.create_shipments_table(connection=self.connection)
        schema.create_user_shipments_table(connection=self.connection)
        schema.create_checkpoints_table(connection=self.connection)
        schema.create_tracking_checks_table(connection=self.connection)
        schema.create_notification_preferences_table(connection=self.connection)
        schema.create_notifications_table(connection=self.connection)
        # Apply up to version 10
        result = apply_migrations(self.connection)
        self.assertEqual(result["current_version"], LATEST_SCHEMA_VERSION)
        return result

    def test_migration_11_applies_cleanly_on_full_v10_db(self):
        self._setup_full_v10_database()

        # Check schema_migrations has version 11
        applied = self.connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
        versions = [row[0] for row in applied]
        self.assertIn(11, versions)
        self.assertEqual(applied[-1][1], "auth_guest_and_notification_ownership")

        # Verify integrity and foreign keys
        self.assertEqual(
            self.connection.execute("PRAGMA integrity_check").fetchone()[0],
            "ok",
        )
        self.assertEqual(
            self.connection.execute("PRAGMA foreign_key_check").fetchall(),
            [],
        )

    def test_migration_11_preserves_existing_notifications_and_receipts(self):
        # Setup base tables up to v10 manually to simulate legacy data
        schema.create_users_table(connection=self.connection)
        schema.create_shipments_table(connection=self.connection)
        schema.create_user_shipments_table(connection=self.connection)
        schema.create_checkpoints_table(connection=self.connection)
        schema.create_tracking_checks_table(connection=self.connection)
        schema.create_notification_preferences_table(connection=self.connection)
        schema.create_notifications_table(connection=self.connection)

        # Seed initial data
        self.connection.execute(
            "INSERT INTO users (user_id, external_user_id, created_at) VALUES (1, 'u_101', '2026-11-20T10:00:00Z')"
        )
        self.connection.execute(
            "INSERT INTO shipments (shipment_id, tracking_number, carrier, provider, created_at, updated_at) "
            "VALUES (10, 'EE123456789IN', 'India Post', 'aftership', '2026-11-20T10:00:00Z', '2026-11-20T10:00:00Z')"
        )
        self.connection.execute(
            "INSERT INTO user_shipments (user_shipment_id, user_id, shipment_id, created_at) "
            "VALUES (100, 1, 10, '2026-11-20T10:00:00Z')"
        )
        self.connection.commit()

        # Run migrations up to 10
        apply_migrations(self.connection)

        # Seed a pre-existing notification with attempt and receipt
        self.connection.execute(
            """INSERT INTO notifications (
                notification_id, user_shipment_id, notification_type, recipient,
                delivery_status, idempotency_key, attempt_count, created_at
            ) VALUES (
                1, 100, 'telegram', '12345678', 'pending', 'legacy-key-001', 1, '2026-11-20T10:00:00Z'
            )"""
        )
        self.connection.execute(
            """INSERT INTO notification_delivery_attempts (
                attempt_id, notification_id, attempt_number, worker_id,
                channel, provider, started_at, outcome
            ) VALUES (
                1001, 1, 1, 'test-worker-1', 'telegram', 'telegram', '2026-11-20T10:01:00Z', 'sent'
            )"""
        )
        self.connection.execute(
            """INSERT INTO notification_delivery_receipts (
                receipt_id, notification_id, attempt_id, provider,
                provider_event_id, provider_message_id, receipt_status,
                provider_event_at, received_at, processed_at
            ) VALUES (
                2001, 1, 1001, 'telegram', 'evt_001', 'msg_001', 'delivered',
                '2026-11-20T10:01:05Z', '2026-11-20T10:01:06Z', '2026-11-20T10:01:07Z'
            )"""
        )
        self.connection.commit()

        # Verify pre-existing records exist
        notif = self.connection.execute("SELECT * FROM notifications WHERE notification_id = 1").fetchone()
        self.assertIsNotNone(notif)

        # Verify child tables join cleanly
        joined = self.connection.execute(
            """SELECT n.notification_id, a.attempt_id, r.receipt_id
               FROM notifications n
               JOIN notification_delivery_attempts a ON a.notification_id = n.notification_id
               JOIN notification_delivery_receipts r ON r.notification_id = n.notification_id
               WHERE n.notification_id = 1"""
        ).fetchone()
        self.assertEqual(joined, (1, 1001, 2001))

        # Check notifications columns
        cols = {
            row[1]: row for row in self.connection.execute('PRAGMA table_info("notifications")').fetchall()
        }
        self.assertIn("guest_subscription_id", cols)
        self.assertIn("user_shipment_id", cols)
        # user_shipment_id is now nullable (notnull == 0)
        self.assertEqual(cols["user_shipment_id"][3], 0)
        self.assertEqual(cols["guest_subscription_id"][3], 0)

    def test_notification_ownership_mutual_exclusion_constraint(self):
        self._setup_full_v10_database()

        # Seed user and shipment
        self.connection.execute(
            "INSERT INTO users (user_id, external_user_id, created_at) VALUES (1, 'u_1', '2026-11-20T10:00:00Z')"
        )
        self.connection.execute(
            "INSERT INTO shipments (shipment_id, tracking_number, carrier, provider, created_at, updated_at) "
            "VALUES (10, 'EE123456789IN', 'India Post', 'aftership', '2026-11-20T10:00:00Z', '2026-11-20T10:00:00Z')"
        )
        self.connection.execute(
            "INSERT INTO user_shipments (user_shipment_id, user_id, shipment_id, created_at) "
            "VALUES (100, 1, 10, '2026-11-20T10:00:00Z')"
        )

        # Seed guest
        self.connection.execute(
            "INSERT INTO guest_notification_identities (guest_identity_id, created_at) "
            "VALUES ('g_uuid_1', '2026-11-20T10:00:00Z')"
        )
        self.connection.execute(
            "INSERT INTO guest_shipment_subscriptions (guest_subscription_id, guest_identity_id, shipment_id, created_at, updated_at) "
            "VALUES (500, 'g_uuid_1', 10, '2026-11-20T10:00:00Z', '2026-11-20T10:00:00Z')"
        )

        # 1. Registered notification succeeds
        self.connection.execute(
            """INSERT INTO notifications (
                user_shipment_id, guest_subscription_id, notification_type,
                recipient, idempotency_key, created_at
            ) VALUES (100, NULL, 'telegram', '12345', 'key_reg', '2026-11-20T10:00:00Z')"""
        )

        # 2. Guest notification succeeds
        self.connection.execute(
            """INSERT INTO notifications (
                user_shipment_id, guest_subscription_id, notification_type,
                recipient, idempotency_key, created_at
            ) VALUES (NULL, 500, 'telegram', '67890', 'key_guest', '2026-11-20T10:00:00Z')"""
        )

        # 3. Both NULL fails CHECK constraint
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """INSERT INTO notifications (
                    user_shipment_id, guest_subscription_id, notification_type,
                    recipient, idempotency_key, created_at
                ) VALUES (NULL, NULL, 'telegram', '00000', 'key_both_null', '2026-11-20T10:00:00Z')"""
            )

        # 4. Both populated fails CHECK constraint
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """INSERT INTO notifications (
                    user_shipment_id, guest_subscription_id, notification_type,
                    recipient, idempotency_key, created_at
                ) VALUES (100, 500, 'telegram', '00000', 'key_both_pop', '2026-11-20T10:00:00Z')"""
            )

    def test_telegram_link_challenges_mutual_exclusion(self):
        self._setup_full_v10_database()

        self.connection.execute(
            "INSERT INTO users (user_id, external_user_id, created_at) VALUES (1, 'u_1', '2026-11-20T10:00:00Z')"
        )
        self.connection.execute(
            "INSERT INTO guest_notification_identities (guest_identity_id, created_at) "
            "VALUES ('g_uuid_1', '2026-11-20T10:00:00Z')"
        )

        # Valid registered challenge
        self.connection.execute(
            """INSERT INTO telegram_link_challenges (
                challenge_id, owner_type, user_id, guest_identity_id,
                challenge_code_hash, expires_at, created_at
            ) VALUES ('c1', 'registered', 1, NULL, 'hash1', '2026-11-20T11:00:00Z', '2026-11-20T10:00:00Z')"""
        )

        # Valid guest challenge
        self.connection.execute(
            """INSERT INTO telegram_link_challenges (
                challenge_id, owner_type, user_id, guest_identity_id,
                challenge_code_hash, expires_at, created_at
            ) VALUES ('c2', 'guest', NULL, 'g_uuid_1', 'hash2', '2026-11-20T11:00:00Z', '2026-11-20T10:00:00Z')"""
        )

        # Invalid: registered with guest_identity_id
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """INSERT INTO telegram_link_challenges (
                    challenge_id, owner_type, user_id, guest_identity_id,
                    challenge_code_hash, expires_at, created_at
                ) VALUES ('c3', 'registered', 1, 'g_uuid_1', 'hash3', '2026-11-20T11:00:00Z', '2026-11-20T10:00:00Z')"""
            )

        # Invalid: guest with user_id
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """INSERT INTO telegram_link_challenges (
                    challenge_id, owner_type, user_id, guest_identity_id,
                    challenge_code_hash, expires_at, created_at
                ) VALUES ('c4', 'guest', 1, 'g_uuid_1', 'hash4', '2026-11-20T11:00:00Z', '2026-11-20T10:00:00Z')"""
            )

    def test_migration_11_is_idempotent(self):
        self._setup_full_v10_database()

        first_count = self.connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        second = apply_migrations(self.connection)

        self.assertEqual(second["applied"], [])
        self.assertTrue(second["already_current"])
        self.assertEqual(second["current_version"], LATEST_SCHEMA_VERSION)
        second_count = self.connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        self.assertEqual(first_count, second_count)


if __name__ == "__main__":
    unittest.main()
