import contextlib
import io
import os
import unittest
from unittest.mock import patch

import httpx
import aftership_client


class AfterShipDiagnosticSanitizationTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "AFTERSHIP_API_KEY": "dummy-test-key",
            "AFTERSHIP_BASE_URL": "https://example.invalid"
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def _assert_http_body_is_not_printed(self, method_name, http_method, *args):
        request = httpx.Request(http_method, "https://example.invalid/trackings")
        response = httpx.Response(500, request=request, text="SENSITIVE_PROVIDER_BODY")
        error = httpx.HTTPStatusError("provider error", request=request, response=response)
        output = io.StringIO()
        with patch.object(aftership_client.httpx, http_method.lower(), side_effect=error):
            with contextlib.redirect_stdout(output):
                self.assertIsNone(getattr(aftership_client, method_name)(*args))
        self.assertIn("500", output.getvalue())
        self.assertNotIn("SENSITIVE_PROVIDER_BODY", output.getvalue())

    def test_get_tracking_http_error_body_is_not_printed(self):
        self._assert_http_body_is_not_printed("get_tracking", "GET", "test-id")

    def test_create_tracking_http_error_body_is_not_printed(self):
        self._assert_http_body_is_not_printed("create_tracking", "POST", "AB123456789IN")

    def test_find_tracking_id_http_error_body_is_not_printed(self):
        self._assert_http_body_is_not_printed("find_tracking_id", "GET", "AB123456789IN")

    def test_connection_exception_text_is_not_printed(self):
        request = httpx.Request("GET", "https://example.invalid/trackings")
        output = io.StringIO()
        error = httpx.RequestError("SENSITIVE_URL_OR_TOKEN", request=request)
        with patch.object(aftership_client.httpx, "get", side_effect=error):
            with contextlib.redirect_stdout(output):
                self.assertIsNone(aftership_client.get_tracking("test-id"))
        self.assertIn("connection request failed", output.getvalue())
        self.assertNotIn("SENSITIVE_URL_OR_TOKEN", output.getvalue())


if __name__ == "__main__":
    unittest.main()
