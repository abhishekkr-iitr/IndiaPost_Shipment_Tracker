"""Tests for guest notification identities, subscriptions, preferences, and Telegram linking."""
import hashlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from database import schema
from database.db_connection import get_db_connection
from database.migrations import apply_migrations
from database.guest_notification_repository import (
    create_guest_notification_identity,
    get_guest_notification_identity,
    revoke_guest_notification_identity,
    create_guest_session,
    get_guest_session,
    revoke_guest_session,
    revoke_all_guest_sessions,
    create_guest_shipment_subscription,
    get_guest_shipment_subscription,
    update_guest_monitoring_status,
    list_guest_subscriptions,
    save_guest_channel_preference,
    get_guest_channel_preference,
    revoke_guest_channel_consent,
    get_eligible_guest_recipients,
)
from database.notification_channel_preference_repository import get_eligible_recipients
from notifications.telegram_linking import (
    create_telegram_link_challenge,
    parse_telegram_update,
    verify_and_consume_telegram_link,
    TelegramLinkingError,
)


class GuestAndTelegramLinkingTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_guest.db"
        self.prev_env = os.environ.get("INDIAPOST_DB_PATH")
        os.environ["INDIAPOST_DB_PATH"] = str(self.db_path)

        conn = get_db_connection()
        try:
            schema.create_users_table(connection=conn)
            schema.create_shipments_table(connection=conn)
            schema.create_user_shipments_table(connection=conn)
            schema.create_checkpoints_table(connection=conn)
            schema.create_tracking_checks_table(connection=conn)
            schema.create_notification_preferences_table(connection=conn)
            schema.create_notifications_table(connection=conn)
            apply_migrations(conn)

            # Seed a sample user and shipment for linking tests
            conn.execute(
                "INSERT INTO users (user_id, external_user_id, created_at) VALUES (1, 'user_101', '2026-11-20T10:00:00Z')"
            )
            conn.execute(
                "INSERT INTO shipments (shipment_id, tracking_number, carrier, provider, current_status, created_at, updated_at) "
                "VALUES (10, 'AB123456789IN', 'India Post', 'aftership', 'In Transit', '2026-11-20T10:00:00Z', '2026-11-20T10:00:00Z')"
            )
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
    # Guest Identity & Sessions
    # -----------------------------------------------------------------------

    def test_guest_identity_lifecycle(self):
        gid = create_guest_notification_identity(notes="Browser guest test")
        self.assertIsInstance(gid, str)

        identity = get_guest_notification_identity(gid)
        self.assertIsNotNone(identity)
        self.assertEqual(identity["guest_identity_id"], gid)
        self.assertEqual(identity["notes"], "Browser guest test")
        self.assertIsNone(identity["revoked_at"])

        # Revoke identity
        revoked = revoke_guest_notification_identity(gid)
        self.assertTrue(revoked)

        updated = get_guest_notification_identity(gid)
        self.assertIsNotNone(updated["revoked_at"])

    def test_guest_session_lifecycle_and_invalidation(self):
        gid = create_guest_notification_identity()
        token_hash = hashlib.sha256(b"guest_token_123").hexdigest()
        expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(timespec="seconds")

        create_guest_session(token_hash, gid, expires_at)

        # Active session lookup succeeds
        sess = get_guest_session(token_hash)
        self.assertIsNotNone(sess)
        self.assertEqual(sess["guest_identity_id"], gid)

        # Revoking identity invalidates session
        revoke_guest_notification_identity(gid)
        self.assertIsNone(get_guest_session(token_hash))

    def test_guest_session_direct_revocation(self):
        gid = create_guest_notification_identity()
        th1 = hashlib.sha256(b"token_1").hexdigest()
        th2 = hashlib.sha256(b"token_2").hexdigest()
        expires_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds")

        create_guest_session(th1, gid, expires_at)
        create_guest_session(th2, gid, expires_at)

        # Revoke one
        self.assertTrue(revoke_guest_session(th1))
        self.assertIsNone(get_guest_session(th1))
        self.assertIsNotNone(get_guest_session(th2))

        # Revoke all for guest
        count = revoke_all_guest_sessions(gid)
        self.assertEqual(count, 1)
        self.assertIsNone(get_guest_session(th2))

    # -----------------------------------------------------------------------
    # Guest Shipment Subscriptions
    # -----------------------------------------------------------------------

    def test_guest_shipment_subscription(self):
        gid = create_guest_notification_identity()
        sub_id = create_guest_shipment_subscription(gid, 10, monitoring_interval_hours=4)
        self.assertIsInstance(sub_id, int)
        self.assertGreater(sub_id, 0)

        sub = get_guest_shipment_subscription(gid, 10)
        self.assertIsNotNone(sub)
        self.assertTrue(sub["monitoring_enabled"])
        self.assertEqual(sub["monitoring_interval_hours"], 4)

        # Update monitoring
        self.assertTrue(update_guest_monitoring_status(sub_id, False))
        sub_updated = get_guest_shipment_subscription(gid, 10)
        self.assertFalse(sub_updated["monitoring_enabled"])

        # Re-subscribing same shipment reactivates monitoring
        create_guest_shipment_subscription(gid, 10)
        sub_reactivated = get_guest_shipment_subscription(gid, 10)
        self.assertTrue(sub_reactivated["monitoring_enabled"])

        # Listing subscriptions includes tracking details
        subs = list_guest_subscriptions(gid)
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["tracking_number"], "AB123456789IN")

    # -----------------------------------------------------------------------
    # Guest Channel Preferences & Consent
    # -----------------------------------------------------------------------

    def test_guest_channel_preferences_and_consent(self):
        gid = create_guest_notification_identity()
        now_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

        pref_id = save_guest_channel_preference(
            guest_identity_id=gid,
            channel="telegram",
            recipient="987654321",
            enabled=True,
            verified_at=now_ts,
            consent_at=now_ts,
        )
        self.assertGreater(pref_id, 0)

        pref = get_guest_channel_preference(gid, "telegram")
        self.assertIsNotNone(pref)
        self.assertEqual(pref["recipient"], "987654321")
        self.assertTrue(pref["enabled"])
        self.assertIsNone(pref["consent_revoked_at"])

        # Channel is eligible for notifications
        eligible = get_eligible_guest_recipients(gid)
        self.assertEqual(len(eligible), 1)
        self.assertEqual(eligible[0]["channel"], "telegram")
        self.assertEqual(eligible[0]["recipient"], "987654321")

        # Revoking consent disables delivery immediately
        self.assertTrue(revoke_guest_channel_consent(gid, "telegram"))
        revoked_pref = get_guest_channel_preference(gid, "telegram")
        self.assertFalse(revoked_pref["enabled"])
        self.assertIsNotNone(revoked_pref["consent_revoked_at"])

        # Now excluded from eligible recipients
        self.assertEqual(get_eligible_guest_recipients(gid), [])

    # -----------------------------------------------------------------------
    # Telegram Inbound Linking
    # -----------------------------------------------------------------------

    def test_telegram_linking_registered_user(self):
        challenge = create_telegram_link_challenge(
            owner_type="registered",
            owner_id=1,
            expires_in_seconds=600,
        )
        self.assertIn("link_code", challenge)
        self.assertIn("deep_link", challenge)
        code = challenge["link_code"]

        # Synthetic incoming Telegram webhook update
        payload = {
            "update_id": 10001,
            "message": {
                "message_id": 55,
                "chat": {"id": 123456789, "type": "private"},
                "from": {"id": 123456789, "first_name": "TestUser"},
                "text": f"/start {code}",
            },
        }

        # Verify and consume
        result = verify_and_consume_telegram_link(code, payload)
        self.assertEqual(result["status"], "linked")
        self.assertEqual(result["owner_type"], "registered")
        self.assertEqual(result["chat_id"], "123456789")

        # Verify registered user's channel preference was updated
        reg_eligible = get_eligible_recipients(1)
        self.assertTrue(any(p["channel"] == "telegram" and p["recipient"] == "123456789" for p in reg_eligible))

        # Replay attempt fails
        with self.assertRaises(TelegramLinkingError):
            verify_and_consume_telegram_link(code, payload)

    def test_telegram_linking_guest(self):
        gid = create_guest_notification_identity()
        challenge = create_telegram_link_challenge(
            owner_type="guest",
            owner_id=gid,
            expires_in_seconds=600,
        )
        code = challenge["link_code"]

        payload = {
            "update_id": 10002,
            "message": {
                "message_id": 56,
                "chat": {"id": -987654321, "type": "group"},
                "text": f"/start {code}",
            },
        }

        result = verify_and_consume_telegram_link(code, payload)
        self.assertEqual(result["status"], "linked")
        self.assertEqual(result["owner_type"], "guest")
        self.assertEqual(result["owner_id"], gid)
        self.assertEqual(result["chat_id"], "-987654321")

        # Verify guest channel preference was updated with consent
        guest_eligible = get_eligible_guest_recipients(gid)
        self.assertEqual(len(guest_eligible), 1)
        self.assertEqual(guest_eligible[0]["recipient"], "-987654321")
        self.assertIsNotNone(guest_eligible[0]["consent_at"])

    def test_telegram_update_parsing_validation(self):
        # Valid payload
        chat_id, param = parse_telegram_update({
            "message": {"chat": {"id": 444555}, "text": "/start abc_123"}
        })
        self.assertEqual(chat_id, "444555")
        self.assertEqual(param, "abc_123")

        # Missing chat ID fails
        with self.assertRaises(TelegramLinkingError):
            parse_telegram_update({"message": {"text": "hello"}})

        # Non-numeric chat ID fails
        with self.assertRaises(TelegramLinkingError):
            parse_telegram_update({"message": {"chat": {"id": "not-numeric"}}})


if __name__ == "__main__":
    unittest.main()
