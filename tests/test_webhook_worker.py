"""Tests for the durable AfterShip webhook background worker."""

import json
import os
import tempfile
import unittest
import uuid

from pathlib import Path
from threading import Event
from unittest.mock import patch

from webhooks.webhook_config import WebhookConfig
from webhooks.webhook_worker import WebhookWorker


class TestWebhookWorker(unittest.TestCase):

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "worker_test.db"

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

        self.config = WebhookConfig(
            enabled=True,
            secret="synthetic-worker-test-secret",
            max_body_bytes=1024 * 1024,
            worker_poll_seconds=0.05,
            max_processing_attempts=5,
            worker_lease_seconds=300,
            retry_delays_seconds=(60, 300, 900, 3600, 21600),
        )

        from database.webhook_event_repository import (
            save_webhook_event,
            get_webhook_event,
        )

        self.save_webhook_event = save_webhook_event
        self.get_webhook_event = get_webhook_event

    def tearDown(self):
        self.environment_patch.stop()
        self.temp_directory.cleanup()

    def _save_event(self, message=None):
        if message is None:
            message = {}

        payload = {
            "ts": 1794825600,
            "event": "tracking_update",
            "event_id": str(uuid.uuid4()),
            "msg": message,
        }
        raw_payload = json.dumps(
            payload,
            separators=(",", ":"),
        ).encode("utf-8")

        result = self.save_webhook_event(
            payload,
            raw_payload,
            provider="aftership",
        )
        return result["event_record_id"]

    def test_worker_generates_unique_worker_id_by_default(self):
        first = WebhookWorker(config=self.config)
        second = WebhookWorker(config=self.config)

        self.assertTrue(first.worker_id)
        self.assertNotEqual(first.worker_id, second.worker_id)

    def test_process_next_event_returns_none_when_queue_is_empty(self):
        worker = WebhookWorker(
            config=self.config,
            worker_id="worker-empty",
        )

        self.assertIsNone(worker.process_next_event())

    def test_worker_processes_persisted_event_end_to_end(self):
        # An event without shipment identifiers is valid and should be
        # durably marked processed without creating a shipment.
        event_record_id = self._save_event(message={})

        worker = WebhookWorker(
            config=self.config,
            worker_id="worker-integration",
        )

        result = worker.process_next_event()

        self.assertIsNotNone(result)
        self.assertEqual(result["event_record_id"], event_record_id)
        self.assertEqual(result["status"], "processed")
        self.assertTrue(result["result"]["processed"])

        record = self.get_webhook_event(event_record_id)
        self.assertEqual(record["status"], "processed")
        self.assertEqual(record["processing_attempts"], 1)
        self.assertIsNone(record["claimed_by"])
        self.assertIsNone(record["lease_expires_at"])

    def test_processing_failure_is_recorded_for_retry(self):
        event_record_id = self._save_event(message={})

        worker = WebhookWorker(
            config=self.config,
            worker_id="worker-failure",
        )

        with patch(
            "webhooks.webhook_worker.process_claimed_webhook_event",
            side_effect=RuntimeError("synthetic processor failure"),
        ):
            result = worker.process_next_event()

        self.assertEqual(result["event_record_id"], event_record_id)
        self.assertEqual(result["status"], "retryable")

        record = self.get_webhook_event(event_record_id)
        self.assertEqual(record["status"], "retryable")
        self.assertEqual(record["processing_attempts"], 1)
        self.assertIsNotNone(record["next_retry_at"])
        self.assertIn(
            "synthetic processor failure",
            record["last_error"],
        )
        self.assertIsNone(record["claimed_by"])

    def test_worker_refuses_to_run_when_disabled(self):
        disabled_config = WebhookConfig(
            enabled=False,
            secret="",
            max_body_bytes=1024,
            worker_poll_seconds=0.05,
            max_processing_attempts=5,
            worker_lease_seconds=300,
            retry_delays_seconds=(60, 300, 900, 3600, 21600),
        )

        worker = WebhookWorker(config=disabled_config)

        with self.assertRaisesRegex(RuntimeError, "disabled"):
            worker.run(stop_event=Event())

    def test_worker_stops_cleanly_when_stop_event_is_set(self):
        worker = WebhookWorker(
            config=self.config,
            worker_id="worker-stop",
        )
        stop_event = Event()
        stop_event.set()

        # The pre-set stop signal prevents a claim or polling iteration.
        worker.run(stop_event=stop_event)

        self.assertTrue(stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
