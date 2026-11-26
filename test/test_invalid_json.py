
import os
from unittest.mock import patch

import httpx


def run_test():
    original_api_key = os.environ.get("AFTERSHIP_API_KEY")
    original_base_url = os.environ.get("AFTERSHIP_BASE_URL")

    try:
        # Dummy configuration for offline testing only.
        os.environ["AFTERSHIP_API_KEY"] = "TEST_ONLY_API_KEY"
        os.environ["AFTERSHIP_BASE_URL"] = (
            "https://api.aftership.com/v4"
        )

        # Import after test configuration is set.
        from aftership_client import get_tracking

        fake_request = httpx.Request(
            "GET",
            "https://api.aftership.com/v4/trackings/test-tracking-id",
        )

        fake_response = httpx.Response(
            200,
            text="This is not a valid JSON response",
            request=fake_request,
        )

        with patch(
            "aftership_client.httpx.get",
            return_value=fake_response,
        ) as mock_get:

            result = get_tracking("test-tracking-id")

        # Invalid JSON should be handled safely.
        assert result is None, (
            "Expected None for an invalid JSON response."
        )

        mock_get.assert_called_once()

        print("Invalid JSON response handled correctly.")
        print("get_tracking() returned:", result)
        print("HTTP Request Verification: PASSED")
        print("\nINVALID JSON TEST: PASSED")

    finally:
        # Restore the original environment variables.
        if original_api_key is None:
            os.environ.pop("AFTERSHIP_API_KEY", None)
        else:
            os.environ["AFTERSHIP_API_KEY"] = original_api_key

        if original_base_url is None:
            os.environ.pop("AFTERSHIP_BASE_URL", None)
        else:
            os.environ["AFTERSHIP_BASE_URL"] = original_base_url


if __name__ == "__main__":
    run_test()
