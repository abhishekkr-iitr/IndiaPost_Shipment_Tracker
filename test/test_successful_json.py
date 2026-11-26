
import os
from unittest.mock import patch

import httpx


def run_test():
    # Provide dummy configuration for this offline test.
    # No real API key or network request is required.
    original_api_key = os.environ.get("AFTERSHIP_API_KEY")
    original_base_url = os.environ.get("AFTERSHIP_BASE_URL")

    try:
        os.environ["AFTERSHIP_API_KEY"] = "TEST_ONLY_API_KEY"
        os.environ["AFTERSHIP_BASE_URL"] = (
            "https://api.aftership.com/v4"
        )

        # Import after setting the test configuration.
        from aftership_client import get_tracking

        fake_request = httpx.Request(
            "GET",
            "https://api.aftership.com/v4/trackings/test-tracking-id",
        )

        fake_response = httpx.Response(
            200,
            json={
                "meta": {
                    "code": 200,
                    "message": "OK",
                },
                "data": {
                    "tracking_number": "TEST123456789IN",
                    "slug": "india-post",
                    "tag": "Delivered",
                },
            },
            request=fake_request,
        )

        with patch(
            "aftership_client.httpx.get",
            return_value=fake_response,
        ) as mock_get:

            result = get_tracking("test-tracking-id")

        # Verify the HTTP client returned the simulated JSON.
        assert result is not None, (
            "get_tracking() returned None."
        )

        assert result["meta"]["code"] == 200

        tracking_data = result["data"]

        assert tracking_data["tracking_number"] == (
            "TEST123456789IN"
        )
        assert tracking_data["slug"] == "india-post"
        assert tracking_data["tag"] == "Delivered"

        # Verify the expected request was made.
        mock_get.assert_called_once()

        request_args = mock_get.call_args

        assert request_args.args[0].endswith(
            "/trackings/test-tracking-id"
        )

        request_headers = request_args.kwargs["headers"]

        assert request_headers["as-api-key"] == (
            "TEST_ONLY_API_KEY"
        )

        print("Successful JSON test completed!")
        print(
            "Consignment Number:",
            tracking_data["tracking_number"],
        )
        print("Carrier:", tracking_data["slug"])
        print("Current Status:", tracking_data["tag"])
        print("HTTP Request Verification: PASSED")
        print("\nSUCCESSFUL JSON TEST: PASSED")

    finally:
        # Restore the environment exactly as it was.
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
