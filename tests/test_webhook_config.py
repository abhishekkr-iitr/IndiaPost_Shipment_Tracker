import unittest
from unittest.mock import patch

from webhooks.webhook_config import load_webhook_config


class TestWebhookConfig(unittest.TestCase):

    def test_default_configuration(self):
        with patch.dict("os.environ", {}, clear=True):
            config = load_webhook_config()

        self.assertFalse(config.enabled)
        self.assertEqual(config.secret, "")
        self.assertEqual(config.max_body_bytes, 1_048_576)
        self.assertEqual(config.worker_poll_seconds, 2.0)
        self.assertEqual(config.max_processing_attempts, 5)
        self.assertEqual(config.worker_lease_seconds, 300)

    def test_enabled_without_secret_fails(self):
        with patch.dict(
            "os.environ",
            {"WEBHOOK_ENABLED": "true"},
            clear=True
        ):
            with self.assertRaisesRegex(
                ValueError, "AFTERSHIP_WEBHOOK_SECRET"
            ):
                load_webhook_config()

    def test_enabled_with_secret(self):
        with patch.dict(
            "os.environ",
            {
                "WEBHOOK_ENABLED": "true",
                "AFTERSHIP_WEBHOOK_SECRET": "synthetic-test-secret",
            },
            clear=True
        ):
            config = load_webhook_config()

        self.assertTrue(config.enabled)
        self.assertEqual(config.secret, "synthetic-test-secret")

    def test_invalid_boolean_fails(self):
        with patch.dict(
            "os.environ",
            {"WEBHOOK_ENABLED": "maybe"},
            clear=True
        ):
            with self.assertRaisesRegex(ValueError, "WEBHOOK_ENABLED"):
                load_webhook_config()

    def test_invalid_body_size_fails(self):
        with patch.dict(
            "os.environ",
            {"WEBHOOK_MAX_BODY_BYTES": "100"},
            clear=True
        ):
            with self.assertRaisesRegex(
                ValueError, "WEBHOOK_MAX_BODY_BYTES"
            ):
                load_webhook_config()

    def test_invalid_worker_poll_interval_fails(self):
        with patch.dict(
            "os.environ",
            {"WEBHOOK_WORKER_POLL_SECONDS": "0"},
            clear=True
        ):
            with self.assertRaisesRegex(
                ValueError, "WEBHOOK_WORKER_POLL_SECONDS"
            ):
                load_webhook_config()

    def test_excessive_attempts_fail(self):
        with patch.dict(
            "os.environ",
            {"WEBHOOK_MAX_PROCESSING_ATTEMPTS": "10"},
            clear=True
        ):
            with self.assertRaisesRegex(
                ValueError, "WEBHOOK_MAX_PROCESSING_ATTEMPTS"
            ):
                load_webhook_config()


if __name__ == "__main__":
    unittest.main()