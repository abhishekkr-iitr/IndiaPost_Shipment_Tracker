"""End-to-end mocked tests for the Telegram notification delivery path.

These tests use a disposable SQLite database and httpx.MockTransport.
They never contact Telegram and never require a real bot token.
"""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from database import schema
from database.db_connection import get_db_connection
from database.migrations import apply_migrations
from database.notification_channel_preference_repository import save_channel_preference
from notifications.delivery_setup import build_delivery_runtime
from notifications.pipeline import NotificationPipeline
from notifications.worker import NotificationWorker


class TelegramWorkerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "telegram_worker.sqlite3")
        self.env = patch.dict(os.environ, {"INDIAPOST_DB_PATH": self.db_path})
        self.env.start()

        connection = get_db_connection()
        schema.create_users_table(connection)
        schema.create_shipments_table(connection)
        schema.create_user_shipments_table(connection)
        schema.create_checkpoints_table(connection)
        schema.create_notification_preferences_table(connection)
        schema.create_notifications_table(connection)
        connection.execute(
            "INSERT INTO users(external_user_id, created_at) VALUES ('test-user', '2026-01-01T00:00:00+00:00')"
        )
        connection.execute(
            """INSERT INTO shipments
               (tracking_number, carrier, provider, created_at, updated_at)
               VALUES ('AB123456789IN', 'india_post', 'aftership', '2026-01-01', '2026-01-01')"""
        )
        connection.execute(
            "INSERT INTO user_shipments(user_id, shipment_id, created_at) VALUES (1, 1, '2026-01-01')"
        )
        connection.commit()
        connection.close()
        apply_migrations()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def _add_pending_notification(self):
        verified = datetime.now(timezone.utc).isoformat()
        save_channel_preference(1, "telegram", "987654321", True, verified)
        connection = get_db_connection()
        cursor = connection.execute(
            """INSERT INTO notifications
               (user_shipment_id, notification_type, recipient, notification_message,
                delivery_status, idempotency_key, created_at)
               VALUES (1, 'telegram', '987654321', 'Shipment status updated.',
                       'pending', 'telegram-integration-1', '2026-01-01')"""
        )
        notification_id = cursor.lastrowid
        connection.commit()
        connection.close()
        return notification_id

    def _run_pipeline(self, handler):
        requests = []

        def recording_handler(request):
            requests.append(request)
            return handler(request)

        client = httpx.Client(transport=httpx.MockTransport(recording_handler))
        runtime = build_delivery_runtime(
            bot_token="123:mock-token",
            http_client=client,
        )
        worker = NotificationWorker(
            "telegram-integration-worker",
            runtime.registry,
            retry_delay_seconds=0,
        )
        pipeline = NotificationPipeline(worker)
        try:
            result = pipeline.dispatch_pending()
        finally:
            runtime.close()
            client.close()
        return result, requests

    def test_pipeline_worker_adapter_transport_and_persistence_success(self):
        notification_id = self._add_pending_notification()

        def handler(request):
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, "/bot123:mock-token/sendMessage")
            body = request.read().decode("utf-8")
            self.assertIn('"chat_id":"987654321"', body)
            self.assertIn('"text":"Shipment status updated."', body)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 4567}})

        result, requests = self._run_pipeline(handler)
        self.assertEqual(result["claimed"], 1)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(len(requests), 1)

        connection = get_db_connection()
        row = connection.execute(
            "SELECT delivery_status, provider_message_id, claimed_by FROM notifications WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()
        connection.close()
        self.assertEqual(row, ("sent", "4567", None))

    def test_temporary_provider_error_is_scheduled_for_retry(self):
        self._add_pending_notification()
        result, requests = self._run_pipeline(
            lambda request: httpx.Response(503, json={"ok": False, "error_code": 503})
        )
        self.assertEqual(result["retry_scheduled"], 1)
        self.assertEqual(len(requests), 1)

        connection = get_db_connection()
        row = connection.execute(
            "SELECT delivery_status, next_retry_at, claimed_by FROM notifications"
        ).fetchone()
        connection.close()
        self.assertEqual(row[0], "pending")
        self.assertIsNotNone(row[1])
        self.assertIsNone(row[2])

    def test_permanent_provider_rejection_is_failed(self):
        self._add_pending_notification()
        result, requests = self._run_pipeline(
            lambda request: httpx.Response(400, json={"ok": False, "error_code": 400})
        )
        self.assertEqual(result["failed"], 1)
        self.assertEqual(len(requests), 1)

        connection = get_db_connection()
        row = connection.execute("SELECT delivery_status, claimed_by FROM notifications").fetchone()
        connection.close()
        self.assertEqual(row, ("failed", None))

    def test_revoked_recipient_authorization_prevents_provider_request(self):
        self._add_pending_notification()
        connection = get_db_connection()
        connection.execute(
            "UPDATE notification_channel_preferences SET enabled = 0 WHERE user_id = 1 AND channel = 'telegram'"
        )
        connection.commit()
        connection.close()

        result, requests = self._run_pipeline(
            lambda request: httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        )
        self.assertEqual(result["suppressed_unauthorized"], 1)
        self.assertEqual(requests, [])


if __name__ == "__main__":
    unittest.main()
