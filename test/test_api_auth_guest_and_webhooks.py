"""Integration tests for FastAPI auth routes, guest notification routes, and Telegram webhooks."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import app
from api.auth_routes import get_auth_service, set_auth_service
from auth.passwordless_service import MockEmailDeliveryProvider, PasswordlessAuthService
from database import schema
from database.db_connection import get_db_connection
from database.migrations import apply_migrations
from notifications.telegram_linking import create_telegram_link_challenge


class APIAuthGuestAndWebhooksTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_api.db"
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

            # Seed test shipment
            conn.execute(
                """INSERT INTO shipments (
                    shipment_id, tracking_number, carrier, provider,
                    current_status, created_at, updated_at
                ) VALUES (
                    10, 'IN123456789XX', 'india-post', 'aftership',
                    'In Transit', '2026-11-20T10:00:00Z', '2026-11-20T10:00:00Z'
                )"""
            )
            conn.commit()
        finally:
            conn.close()

        # Wire up mock delivery provider for auth service
        self.mock_email_provider = MockEmailDeliveryProvider()
        self.auth_service = PasswordlessAuthService(email_provider=self.mock_email_provider)
        set_auth_service(self.auth_service)

        self.prev_webhook_secret = os.environ.pop("TELEGRAM_WEBHOOK_SECRET", None)
        self.client = TestClient(app)

    def tearDown(self):
        set_auth_service(None)
        if self.prev_webhook_secret is not None:
            os.environ["TELEGRAM_WEBHOOK_SECRET"] = self.prev_webhook_secret
        if self.prev_env is not None:
            os.environ["INDIAPOST_DB_PATH"] = self.prev_env
        else:
            os.environ.pop("INDIAPOST_DB_PATH", None)
        self.temp_dir.cleanup()

    # -----------------------------------------------------------------------
    # Passwordless Authentication Routes
    # -----------------------------------------------------------------------

    def test_auth_challenge_request_and_verify_flow(self):
        # 1. Request challenge
        req_res = self.client.post("/api/auth/challenge", json={"email": "pilot.user@example.com"})
        self.assertEqual(req_res.status_code, 200)
        data = req_res.json()
        self.assertTrue(data["success"])
        challenge_id = data["challenge_id"]

        # Delivered code via mock email provider
        self.assertEqual(len(self.mock_email_provider.sent_challenges), 1)
        code = self.mock_email_provider.latest_code
        self.assertIsNotNone(code)

        # 2. Verify with wrong code -> 401
        bad_res = self.client.post("/api/auth/verify", json={"challenge_id": challenge_id, "code": "000000"})
        self.assertEqual(bad_res.status_code, 401)

        # 3. Verify with correct code -> 200 and session cookie
        verify_res = self.client.post("/api/auth/verify", json={"challenge_id": challenge_id, "code": code})
        self.assertEqual(verify_res.status_code, 200)
        verify_data = verify_res.json()
        self.assertTrue(verify_data["success"])
        token = verify_data["token"]
        self.assertIn("india_post_auth_session", verify_res.headers.get("set-cookie", ""))

        # 4. Check /api/auth/me with Cookie
        me_cookie_res = self.client.get("/api/auth/me", headers={"Cookie": f"india_post_auth_session={token}"})
        self.assertEqual(me_cookie_res.status_code, 200)
        self.assertEqual(me_cookie_res.json()["user_id"], verify_data["user_id"])

        # 5. Check /api/auth/me with Bearer Header
        me_header_res = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(me_header_res.status_code, 200)
        self.assertEqual(me_header_res.json()["user_id"], verify_data["user_id"])

        # 6. Logout
        logout_res = self.client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(logout_res.status_code, 200)

        # 7. Access after logout -> 401
        me_after_res = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(me_after_res.status_code, 401)

    def test_auth_challenge_rate_limiting(self):
        for _ in range(3):
            res = self.client.post("/api/auth/challenge", json={"email": "ratelimit@example.com"})
            self.assertEqual(res.status_code, 200)

        # 4th request exceeds rate limit of 3 active pending challenges
        blocked_res = self.client.post("/api/auth/challenge", json={"email": "ratelimit@example.com"})
        self.assertEqual(blocked_res.status_code, 429)

    # -----------------------------------------------------------------------
    # Guest Notification Routes
    # -----------------------------------------------------------------------

    def test_guest_subscription_and_preferences(self):
        # 1. Subscribe guest with email
        sub_res = self.client.post(
            "/api/guest/subscribe",
            json={
                "tracking_number": "IN123456789XX",
                "carrier": "india-post",
                "channel": "email",
                "recipient": "guest.notify@example.com",
                "consent": True,
            },
        )
        self.assertEqual(sub_res.status_code, 200)
        sub_data = sub_res.json()
        self.assertTrue(sub_data["success"])
        self.assertEqual(sub_data["shipment_id"], 10)
        guest_sub_id = sub_data["guest_subscription_id"]

        cookie = sub_res.cookies.get("india_post_guest_session")
        self.assertIsNotNone(cookie)

        # 2. List guest subscriptions
        list_res = self.client.get("/api/guest/subscriptions", headers={"Cookie": f"india_post_guest_session={cookie}"})
        self.assertEqual(list_res.status_code, 200)
        subs = list_res.json()["subscriptions"]
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["guest_subscription_id"], guest_sub_id)
        self.assertTrue(subs[0]["monitoring_enabled"])

        # 3. Unsubscribe
        unsub_res = self.client.post(
            "/api/guest/unsubscribe",
            json={"guest_subscription_id": guest_sub_id},
            headers={"Cookie": f"india_post_guest_session={cookie}"},
        )
        self.assertEqual(unsub_res.status_code, 200)
        self.assertFalse(unsub_res.json()["monitoring_enabled"])

    def test_guest_subscribe_requires_explicit_consent(self):
        res = self.client.post(
            "/api/guest/subscribe",
            json={
                "tracking_number": "IN123456789XX",
                "carrier": "india-post",
                "channel": "email",
                "recipient": "noconsent@example.com",
                "consent": False,
            },
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Explicit consent", res.json()["detail"])

    def test_guest_subscribe_telegram_requests_deep_link(self):
        res = self.client.post(
            "/api/guest/subscribe",
            json={
                "tracking_number": "IN123456789XX",
                "carrier": "india-post",
                "channel": "telegram",
                "consent": True,
            },
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["requires_telegram_link"])
        self.assertTrue(data["telegram_deep_link"].startswith("https://t.me/"))

    # -----------------------------------------------------------------------
    # Inbound Telegram Webhook Routes
    # -----------------------------------------------------------------------

    def test_telegram_webhook_secret_header_enforcement(self):
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": "secret_token_abc123"}):
            # Missing header -> 403
            res_no_header = self.client.post("/api/webhooks/telegram", json={"update_id": 1})
            self.assertEqual(res_no_header.status_code, 403)

            # Wrong header -> 403
            res_bad_header = self.client.post(
                "/api/webhooks/telegram",
                headers={"X-Telegram-Bot-Api-Secret-Token": "wrong_token"},
                json={"update_id": 1},
            )
            self.assertEqual(res_bad_header.status_code, 403)

            # Valid header -> passes security check
            res_ok = self.client.post(
                "/api/webhooks/telegram",
                headers={"X-Telegram-Bot-Api-Secret-Token": "secret_token_abc123"},
                json={
                    "update_id": 1,
                    "message": {
                        "message_id": 100,
                        "chat": {"id": 12345678},
                        "text": "hello",
                    },
                },
            )
            self.assertEqual(res_ok.status_code, 200)
            self.assertEqual(res_ok.json()["status"], "ignored")

    def test_telegram_webhook_inbound_linking_flow(self):
        # 1. Create a guest telegram link challenge
        guest_sub_res = self.client.post(
            "/api/guest/telegram-link",
        )
        self.assertEqual(guest_sub_res.status_code, 200)
        link_data = guest_sub_res.json()
        link_code = link_data["link_code"]

        # 2. Simulate Telegram inbound /start <link_code> webhook
        update_payload = {
            "update_id": 99999,
            "message": {
                "message_id": 101,
                "chat": {"id": 88776655},
                "text": f"/start {link_code}",
            },
        }

        res = self.client.post("/api/webhooks/telegram", json=update_payload)
        self.assertEqual(res.status_code, 200)
        resp_data = res.json()
        self.assertTrue(resp_data["ok"])
        self.assertEqual(resp_data["status"], "linked")
        self.assertEqual(resp_data["chat_id"], "88776655")
        self.assertEqual(resp_data["owner_type"], "guest")

        # 3. Replay attack: submitting same code again fails
        replay_res = self.client.post("/api/webhooks/telegram", json=update_payload)
        self.assertEqual(replay_res.status_code, 200)
        self.assertFalse(replay_res.json()["ok"])
        self.assertEqual(replay_res.json()["status"], "link_failed")


if __name__ == "__main__":
    unittest.main()
