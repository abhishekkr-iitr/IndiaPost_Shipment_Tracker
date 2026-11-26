import unittest

from api.conversation_routes import _safe_exception_metadata


class DiagnosticMetadataTests(unittest.TestCase):
    def test_extracts_only_safe_openai_error_attributes(self):
        class FakeAPIError(Exception):
            status_code = 401
            code = "invalid_api_key"
            type = "authentication_error"
            request_id = "req_test_123"

        error = FakeAPIError("THIS MESSAGE MUST NOT BE LOGGED")
        result = _safe_exception_metadata(error)

        self.assertEqual(result["cause_type"], "FakeAPIError")
        self.assertEqual(result["status_code"], 401)
        self.assertEqual(result["api_error_code"], "invalid_api_key")
        self.assertEqual(result["api_error_type"], "authentication_error")
        self.assertEqual(result["api_request_id"], "req_test_123")
        self.assertNotIn("THIS MESSAGE", repr(result))

    def test_ignores_unexpected_attribute_types(self):
        class OddError(Exception):
            status_code = "401"
            code = object()
            type = None
            request_id = object()

        result = _safe_exception_metadata(OddError("sensitive message"))

        self.assertEqual(result, {"cause_type": "OddError"})

    def test_bounds_string_metadata(self):
        class LongError(Exception):
            code = "x" * 300

        result = _safe_exception_metadata(LongError())
        self.assertEqual(len(result["api_error_code"]), 160)


if __name__ == "__main__":
    unittest.main()
