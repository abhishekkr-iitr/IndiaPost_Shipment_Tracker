"""Comprehensive unit and integration tests for registered passwordless authentication."""
import hashlib
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from auth.passwordless_service import (
    PasswordlessAuthService,
    MockEmailDeliveryProvider,
    AuthenticationError,
    RateLimitExceededError,
)
from database import schema
from database.auth_repository import (
    create_auth_challenge,
    get_auth_challenge,
    record_challenge_attempt,
    consume_auth_challenge,
    get_or_create_user_by_email,
    get_user_auth_identity,
    create_auth_session,
    get_auth_session,
    revoke_auth_session,
    revoke_all_user_sessions,
    normalize_email,
)
from database.db_connection import get_db_connection
from database.migrations import apply_migrations


class PasswordlessAuthenticationTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_auth.db"
        self.prev_env = os.environ.get("INDIAPOST_DB_PATH")
        os.environ["INDIAPOST_DB_PATH"] = str(self.db_path)

        conn = get_db_connection()
        try:
            # Setup full schema up to latest migration
            schema.create_users_table(connection=conn)
            schema.create_shipments_table(connection=conn)
            schema.create_user_shipments_table(connection=conn)
            schema.create_checkpoints_table(connection=conn)
            schema.create_tracking_checks_table(connection=conn)
            schema.create_notification_preferences_table(connection=conn)
            schema.create_notifications_table(connection=conn)
            apply_migrations(conn)
        finally:
            conn.close()

        self.mock_provider = MockEmailDeliveryProvider()
        self.auth_service = PasswordlessAuthService(
            email_provider=self.mock_provider,
            challenge_ttl_seconds=300,
            session_ttl_seconds=86400,
            max_attempts=3,
            rate_limit_max_active=2,
        )

    def tearDown(self):
        if self.prev_env is not None:
            os.environ["INDIAPOST_DB_PATH"] = self.prev_env
        else:
            os.environ.pop("INDIAPOST_DB_PATH", None)
        self.temp_dir.cleanup()

    def test_email_normalization(self):
        self.assertEqual(normalize_email("  User@Example.COM  "), "user@example.com")
        self.assertEqual(normalize_email("test.name+tag@sub.domain.co.in"), "test.name+tag@sub.domain.co.in")

        for invalid in ["not-an-email", "@no-local.com", "no-at.com", "", "spaces in@email.com", 123]:
            with self.assertRaises(ValueError):
                normalize_email(invalid)

    def test_challenge_creation_and_attempt_limits(self):
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="seconds")
        code_hash = hashlib.sha256(b"123456").hexdigest()

        cid = create_auth_challenge(
            identifier="test@example.com",
            challenge_hash=code_hash,
            expires_at=expires_at,
            max_attempts=2,
        )

        ch = get_auth_challenge(cid)
        self.assertIsNotNone(ch)
        self.assertEqual(ch["identifier"], "test@example.com")
        self.assertEqual(ch["challenge_hash"], code_hash)
        self.assertEqual(ch["attempt_count"], 0)
        self.assertEqual(ch["max_attempts"], 2)
        self.assertIsNone(ch["consumed_at"])

        # Attempt 1
        count1 = record_challenge_attempt(cid)
        self.assertEqual(count1, 1)

        # Attempt 2
        count2 = record_challenge_attempt(cid)
        self.assertEqual(count2, 2)

        # Cannot consume when max attempts reached
        consumed = consume_auth_challenge(cid)
        self.assertFalse(consumed)

    def test_challenge_single_use_consumption(self):
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="seconds")
        code_hash = hashlib.sha256(b"654321").hexdigest()

        cid = create_auth_challenge(
            identifier="single@example.com",
            challenge_hash=code_hash,
            expires_at=expires_at,
        )

        # First consumption succeeds
        self.assertTrue(consume_auth_challenge(cid))

        # Replay attempt fails
        self.assertFalse(consume_auth_challenge(cid))

        # Check DB record has consumed_at set
        ch = get_auth_challenge(cid)
        self.assertIsNotNone(ch["consumed_at"])

    def test_expired_challenge_cannot_be_consumed(self):
        past_expires_at = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(timespec="seconds")
        code_hash = hashlib.sha256(b"999999").hexdigest()

        cid = create_auth_challenge(
            identifier="expired@example.com",
            challenge_hash=code_hash,
            expires_at=past_expires_at,
        )

        self.assertFalse(consume_auth_challenge(cid))

    def test_canonical_user_id_derivation(self):
        email = "citizen@example.com"

        # First login creates user and identity mapping
        user_id_1 = get_or_create_user_by_email(email)
        self.assertIsInstance(user_id_1, int)
        self.assertGreater(user_id_1, 0)

        identity = get_user_auth_identity(email)
        self.assertIsNotNone(identity)
        self.assertEqual(identity["user_id"], user_id_1)
        self.assertEqual(identity["identifier"], email)

        # Second login with different casing returns identical canonical user_id
        user_id_2 = get_or_create_user_by_email("  CITIZEN@Example.Com ")
        self.assertEqual(user_id_1, user_id_2)

        conn = get_db_connection()
        try:
            user_rows = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(user_rows, 1)

    def test_session_lifecycle_and_revocation(self):
        user_id = get_or_create_user_by_email("session@example.com")
        token_hash = hashlib.sha256(b"opaque_bearer_token").hexdigest()
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")

        create_auth_session(token_hash, user_id, expires_at)

        # Active session lookup succeeds
        sess = get_auth_session(token_hash)
        self.assertIsNotNone(sess)
        self.assertEqual(sess["user_id"], user_id)
        self.assertIsNone(sess["revoked_at"])

        # Revoke session
        self.assertTrue(revoke_auth_session(token_hash))

        # Looking up revoked session returns None
        self.assertIsNone(get_auth_session(token_hash))

        # Re-revoking returns False
        self.assertFalse(revoke_auth_session(token_hash))

    def test_revoke_all_user_sessions(self):
        user_id = get_or_create_user_by_email("multi@example.com")
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")

        th1 = hashlib.sha256(b"token_1").hexdigest()
        th2 = hashlib.sha256(b"token_2").hexdigest()

        create_auth_session(th1, user_id, expires_at)
        create_auth_session(th2, user_id, expires_at)

        self.assertIsNotNone(get_auth_session(th1))
        self.assertIsNotNone(get_auth_session(th2))

        revoked_count = revoke_all_user_sessions(user_id)
        self.assertEqual(revoked_count, 2)

        self.assertIsNone(get_auth_session(th1))
        self.assertIsNone(get_auth_session(th2))

    def test_passwordless_service_flow_success(self):
        email = "user.happy@example.com"

        # Request challenge
        req = self.auth_service.request_login_challenge(email)
        challenge_id = req["challenge_id"]
        self.assertIn("expires_at", req)

        # Mock delivery received code
        self.assertEqual(len(self.mock_provider.sent_challenges), 1)
        code = self.mock_provider.latest_code
        self.assertIsNotNone(code)
        self.assertEqual(len(code), 6)

        # Verify challenge and receive authenticated session
        auth_result = self.auth_service.verify_challenge_and_create_session(challenge_id, code)
        self.assertIn("token", auth_result)
        self.assertIn("user_id", auth_result)
        self.assertEqual(auth_result["email"], email)

        # Authenticate session with bearer token
        session_info = self.auth_service.authenticate_session(auth_result["token"])
        self.assertIsNotNone(session_info)
        self.assertEqual(session_info["user_id"], auth_result["user_id"])

        # Logout session
        logged_out = self.auth_service.logout_session(auth_result["token"])
        self.assertTrue(logged_out)

        # Post-logout session is invalid
        self.assertIsNone(self.auth_service.authenticate_session(auth_result["token"]))

    def test_passwordless_service_invalid_code_and_lockout(self):
        email = "lockout@example.com"
        req = self.auth_service.request_login_challenge(email)
        cid = req["challenge_id"]

        # Attempt 1: wrong code
        with self.assertRaises(AuthenticationError) as ctx1:
            self.auth_service.verify_challenge_and_create_session(cid, "000000")
        self.assertIn("2 attempt(s) remaining", str(ctx1.exception))

        # Attempt 2: wrong code
        with self.assertRaises(AuthenticationError) as ctx2:
            self.auth_service.verify_challenge_and_create_session(cid, "111111")
        self.assertIn("1 attempt(s) remaining", str(ctx2.exception))

        # Attempt 3: wrong code -> lockout
        with self.assertRaises(AuthenticationError) as ctx3:
            self.auth_service.verify_challenge_and_create_session(cid, "222222")
        self.assertIn("Maximum attempts reached", str(ctx3.exception))

        # Even with correct code now, attempt is rejected
        correct_code = self.mock_provider.latest_code
        with self.assertRaises(AuthenticationError) as ctx4:
            self.auth_service.verify_challenge_and_create_session(cid, correct_code)
        self.assertIn("Maximum verification attempts exceeded", str(ctx4.exception))

    def test_passwordless_service_rate_limiting(self):
        email = "ratelimit@example.com"

        # First request succeeds
        self.auth_service.request_login_challenge(email)
        # Second request succeeds (max active = 2)
        self.auth_service.request_login_challenge(email)

        # Third request exceeds limit
        with self.assertRaises(RateLimitExceededError):
            self.auth_service.request_login_challenge(email)


if __name__ == "__main__":
    unittest.main()
