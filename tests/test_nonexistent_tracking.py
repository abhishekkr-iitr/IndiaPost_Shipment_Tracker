
import os
from unittest.mock import patch


def run_test():
    original_api_key = os.environ.get("AFTERSHIP_API_KEY")
    original_base_url = os.environ.get("AFTERSHIP_BASE_URL")

    try:
        # Dummy configuration for offline testing only.
        os.environ["AFTERSHIP_API_KEY"] = "TEST_ONLY_API_KEY"
        os.environ["AFTERSHIP_BASE_URL"] = (
            "https://api.aftership.com/v4"
        )

        # Import after setting test configuration.
        from tracking.tracking_service import track_shipment

        consignment_number = "XX000000000IN"

        print("\nTesting non-existent tracking number...\n")

        # Simulate AfterShip not finding an existing tracking ID.
        with patch(
            "tracking.tracking_service.find_tracking_id",
            return_value=None,
        ) as mock_find:

            with patch(
                "tracking.tracking_service.get_tracking",
            ) as mock_get:

                tracking_data = track_shipment(
                    consignment_number
                )

        # Verify the expected behaviour.
        assert tracking_data is None, (
            "Expected no tracking information for a nonexistent "
            "consignment."
        )

        mock_find.assert_called_once_with(consignment_number)

        # Tracking retrieval must not happen without a tracking ID.
        mock_get.assert_not_called()

        print("\nTest completed successfully!")
        print("No tracking information was returned.")
        print("AfterShip tracking ID lookup was attempted.")
        print("No tracking retrieval request was made.")
        print("The application handled the missing record.")
        print("\nNONEXISTENT TRACKING TEST: PASSED")

    finally:
        # Restore the original environment.
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
