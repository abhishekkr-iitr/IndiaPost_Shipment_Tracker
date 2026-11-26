"""Integration tests for the AfterShip FastAPI webhook receiver."""

import base64
import hashlib
import hmac
import json
import os
import tempfile
import unittest
import uuid

from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from webhooks.webhook_config import WebhookConfig


class TestWebhookReceiver(unittest.TestCase):

    SECRET = "synthetic-test-webhook-secret"

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "receiver_test.db"

        self.environment_patch = patch.dict(
            os.environ,
            {"INDIAPOST_DB_PATH": str(self.database_path)},
        )
        self.environment_patch.start()

        from database.db_connection import get_db_connection
        from database.migrations import apply_migrations

        connection = get_db_connection()
        try:
            apply_migrations(connection)
        finally:
            connection.close()

        from webhooks.webhook_receiver import create_app

        self.create_app = create_app
        self.config = WebhookConfig(
            enabled=True,
            secret=self.SECRET,
            max_body_bytes=1024,
            worker_poll_seconds=0.2,
            max_processing_attempts=5,
            worker_lease_seconds=300,
            retry_delays_seconds=(60, 300, 900, 3600, 21600),
        )
        self.client = TestClient(self.create_app(self.config))

        self.payload = {
            "ts": 1794825600,
            "event": "tracking_update",
            "event_id": str(uuid.uuid4()),
            "msg": {
                "id": "synthetic-provider-tracking-id",
                "tracking_number": "TEST123456",
                "slug": "india-post",
                "tag": "InTransit",
                "checkpoints": [],
            },
        }
        self.raw_body = json.dumps(
            self.payload,
            separators=(",", ":"),
        ).encode("utf-8")

    def tearDown(self):
        self.environment_patch.stop()
        self.temp_directory.cleanup()

    def _signature(self, body=None):
        if body is None:
            body = self.raw_body

        digest = hmac.new(
            self.SECRET.encode("utf-8"),
            body,
            hashlib.sha256,
        ).digest()

        return base64.b64encode(digest).decode("ascii")

    def _post(self, body=None, signature=None, client=None):
        if body is None:
            body = self.raw_body

        if signature is None:
            signature = self._signature(body)

        if client is None:
            client = self.client

        return client.post(
            "/webhooks/aftership",
            content=body,
            headers={
                "content-type": "application/json",
                "aftership-hmac-sha256": signature,
            },
        )

    def test_valid_event_is_persisted_and_returns_202(self):
        response = self._post()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            response.json(),
            {"accepted": True, "duplicate": False},
        )

        from database.db_connection import get_db_connection

        connection = get_db_connection()
        try:
            row = connection.execute(
                """
                SELECT provider_event_id, status, raw_payload
                FROM webhook_events
                """
            ).fetchone()
        finally:
            connection.close()

        self.assertIsNotNone(row)
        self.assertEqual(row[0], self.payload["event_id"])
        self.assertEqual(row[1], "received")
        self.assertEqual(row[2], self.raw_body.decode("utf-8"))

    def test_duplicate_delivery_returns_200_and_creates_one_record(self):
        first = self._post()
        second = self._post()

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            second.json(),
            {"accepted": True, "duplicate": True},
        )

        from database.db_connection import get_db_connection

        connection = get_db_connection()
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM webhook_events"
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(count, 1)

    def test_invalid_signature_is_rejected_without_persistence(self):
        response = self._post(signature="invalid-signature")

        self.assertEqual(response.status_code, 401)

        from database.db_connection import get_db_connection

        connection = get_db_connection()
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM webhook_events"
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(count, 0)

    def test_missing_signature_is_rejected(self):
        response = self.client.post(
            "/webhooks/aftership",
            content=self.raw_body,
            headers={"content-type": "application/json"},
        )

        self.assertEqual(response.status_code, 401)

    def test_empty_body_is_rejected(self):
        response = self._post(body=b"")

        self.assertEqual(response.status_code, 400)

    def test_malformed_signed_payload_is_rejected(self):
        malformed_body = b'{"event":'

        response = self._post(
            body=malformed_body,
            signature=self._signature(malformed_body),
        )

        self.assertEqual(response.status_code, 400)

    def test_oversized_request_is_rejected(self):
        oversized_body = b"x" * (self.config.max_body_bytes + 1)

        response = self._post(
            body=oversized_body,
            signature=self._signature(oversized_body),
        )

        self.assertEqual(response.status_code, 413)

    def test_disabled_receiver_returns_503(self):
        disabled_config = WebhookConfig(
            enabled=False,
            secret="",
            max_body_bytes=1024,
            worker_poll_seconds=0.2,
            max_processing_attempts=5,
            worker_lease_seconds=300,
            retry_delays_seconds=(60, 300, 900, 3600, 21600),
        )
        disabled_client = TestClient(
            self.create_app(disabled_config)
        )

        response = self._post(client=disabled_client)

        self.assertEqual(response.status_code, 503)

    def test_persistence_failure_returns_503_without_internal_details(self):
        with patch(
            "webhooks.webhook_receiver.save_webhook_event",
            side_effect=RuntimeError("synthetic database failure"),
        ):
            response = self._post()

        self.assertEqual(response.status_code, 503)
        self.assertNotIn(
            "synthetic database failure",
            response.text,
        )
        self.assertEqual(
            response.json()["detail"],
            "Unable to durably accept webhook event.",
        )


if __name__ == "__main__":
    unittest.main()
