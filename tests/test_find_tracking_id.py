
from unittest.mock import patch, Mock
import os

from aftership_client import find_tracking_id


def make_response(payload):
    """Create a simulated successful HTTP response."""
    response = Mock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


@patch.dict(os.environ, {
    "AFTERSHIP_API_KEY": "TEST_ONLY_API_KEY",
    "AFTERSHIP_BASE_URL": "https://api.aftership.com/v4",
})
def run_test():

    consignment_number = "TEST123456789IN"
    expected_tracking_id = "TEST_AFTERSHIP_ID_001"

    # TEST 1: Successful exact tracking-number match.
    simulated_response = {
        "data": {
            "trackings": [
                {
                    "tracking_number": "OTHER123456789IN",
                    "id": "WRONG_TRACKING_ID",
                },
                {
                    "tracking_number": consignment_number,
                    "id": expected_tracking_id,
                },
            ]
        }
    }

    with patch(
        "aftership_client.httpx.get",
        return_value=make_response(simulated_response),
    ) as mock_get:

        tracking_id = find_tracking_id(consignment_number)

    assert tracking_id == expected_tracking_id, (
        "Expected tracking ID was not returned"
    )

    # Verify that the request used the correct parameters.
    request_kwargs = mock_get.call_args.kwargs

    assert request_kwargs["params"] == {
        "tracking_numbers": consignment_number,
        "slug": "india-post",
    }

    assert request_kwargs["timeout"] == 30.0

    assert "as-api-key" in request_kwargs["headers"]

    print("\nTEST 1: Successful Tracking-ID Lookup: PASSED")
    print("Consignment Number:", consignment_number)
    print("Tracking ID:", tracking_id)

    # TEST 2: No matching tracking record.
    no_match_response = {
        "data": {
            "trackings": [
                {
                    "tracking_number": "ANOTHER123456IN",
                    "id": "ANOTHER_TRACKING_ID",
                }
            ]
        }
    }

    with patch(
        "aftership_client.httpx.get",
        return_value=make_response(no_match_response),
    ):
        missing_tracking_id = find_tracking_id(
            consignment_number
        )

    assert missing_tracking_id is None

    print("\nTEST 2: No Matching Tracking Record: PASSED")

    # TEST 3: Empty tracking list.
    empty_response = {
        "data": {
            "trackings": []
        }
    }

    with patch(
        "aftership_client.httpx.get",
        return_value=make_response(empty_response),
    ):
        empty_result = find_tracking_id(consignment_number)

    assert empty_result is None

    print("\nTEST 3: Empty Tracking List: PASSED")

    # TEST 4: Missing data structure.
    invalid_response = {}

    with patch(
        "aftership_client.httpx.get",
        return_value=make_response(invalid_response),
    ):
        invalid_result = find_tracking_id(consignment_number)

    assert invalid_result is None

    print("\nTEST 4: Missing Response Data: PASSED")

    print("\nFIND TRACKING ID TEST: PASSED")


if __name__ == "__main__":
    run_test()
