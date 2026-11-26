"""Offline tests for optional AfterShip configuration and safe request gating."""
import importlib
import os
import sys
from unittest import TestCase
from unittest.mock import patch


class AfterShipConfigurationTests(TestCase):
    def test_module_imports_without_aftership_credentials(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AFTERSHIP_API_KEY", None)
            os.environ.pop("AFTERSHIP_BASE_URL", None)
            sys.modules.pop("aftership_client", None)
            module = importlib.import_module("aftership_client")
            self.assertTrue(callable(module.find_tracking_id))

    def test_missing_configuration_prevents_http_request(self):
        import aftership_client

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AFTERSHIP_API_KEY", None)
            os.environ.pop("AFTERSHIP_BASE_URL", None)
            with patch.object(aftership_client.httpx, "get") as mock_get:
                self.assertIsNone(aftership_client.find_tracking_id("TEST123456789IN"))
                mock_get.assert_not_called()

    def test_incomplete_configuration_prevents_http_request(self):
        import aftership_client

        with patch.dict(os.environ, {"AFTERSHIP_API_KEY": "TEST_ONLY_API_KEY"}, clear=False):
            os.environ.pop("AFTERSHIP_BASE_URL", None)
            with patch.object(aftership_client.httpx, "get") as mock_get:
                self.assertIsNone(aftership_client.get_tracking("TEST_TRACKING_ID"))
                mock_get.assert_not_called()

    def test_configured_request_uses_environment_values(self):
        import aftership_client
        import httpx

        request = httpx.Request("GET", "https://api.aftership.com/v4/trackings/TEST_ID")
        response = httpx.Response(200, json={"data": {"trackings": []}}, request=request)
        with patch.dict(os.environ, {
            "AFTERSHIP_API_KEY": "TEST_ONLY_API_KEY",
            "AFTERSHIP_BASE_URL": "https://api.aftership.com/v4/",
        }, clear=False):
            with patch.object(aftership_client.httpx, "get", return_value=response) as mock_get:
                self.assertIsNone(aftership_client.find_tracking_id("TEST123456789IN"))
                self.assertEqual(mock_get.call_args.args[0], "https://api.aftership.com/v4/trackings")
                self.assertEqual(mock_get.call_args.kwargs["headers"]["as-api-key"], "TEST_ONLY_API_KEY")

    def test_invalid_base_urls_prevent_http_request(self):
        import aftership_client

        invalid_urls = [
            "http://api.aftership.com/v4",
            "not-a-url",
            "https:///v4",
            "https://",
            "https://api.aftership.com/v4?unexpected=1",
            "https://api.aftership.com/v4#fragment",
            "https://user:password@api.aftership.com/v4",
            "https://api.aftership.com:invalid/v4",
            "https://api.aftership.com:70000/v4",
            "https://api.aftership.com/v4 path",
        ]

        for base_url in invalid_urls:
            with self.subTest(base_url=base_url):
                with patch.dict(os.environ, {
                    "AFTERSHIP_API_KEY": "TEST_ONLY_API_KEY",
                    "AFTERSHIP_BASE_URL": base_url,
                }, clear=False):
                    with patch.object(aftership_client.httpx, "get") as mock_get:
                        self.assertIsNone(
                            aftership_client.get_tracking("TEST_TRACKING_ID")
                        )
                        mock_get.assert_not_called()

    def test_valid_https_base_url_with_path_and_trailing_slash_is_accepted(self):
        import aftership_client
        import httpx

        request = httpx.Request(
            "GET",
            "https://api.aftership.com/v4/trackings/TEST_ID",
        )
        response = httpx.Response(
            200,
            json={"data": {"trackings": []}},
            request=request,
        )
        with patch.dict(os.environ, {
            "AFTERSHIP_API_KEY": "TEST_ONLY_API_KEY",
            "AFTERSHIP_BASE_URL": "https://api.aftership.com/v4/",
        }, clear=False):
            with patch.object(
                aftership_client.httpx,
                "get",
                return_value=response,
            ) as mock_get:
                self.assertIsNone(
                    aftership_client.find_tracking_id("TEST123456789IN")
                )
                self.assertEqual(
                    mock_get.call_args.args[0],
                    "https://api.aftership.com/v4/trackings",
                )
