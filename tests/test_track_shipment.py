
from unittest.mock import patch

from tracking.tracking_service import track_shipment


def run_test():

    test_consignment_number = "TEST123456789IN"
    test_tracking_id = "TEST_AFTERSHIP_TRACKING_ID"

    # Simulated AfterShip response.
    simulated_response = {
        "data": {
            "tracking_number": test_consignment_number,
            "slug": "india-post",
            "tag": "InTransit",
            "subtag_message": "Shipment is moving through the network",
            "origin_raw_location": "Delhi, India",
            "destination_raw_location": "Bengaluru, India",
            "shipment_pickup_date": "2026-11-12T09:00:00+05:30",
            "shipment_delivery_date": None,
            "checkpoints": [
                {
                    "checkpoint_time": "2026-11-12T10:00:00+05:30",
                    "location": "Delhi Sorting Office",
                    "tag": "InTransit",
                    "message": "Item dispatched",
                },
                {
                    "checkpoint_time": "2026-11-12T15:00:00+05:30",
                    "location": "Bengaluru Sorting Office",
                    "tag": "InTransit",
                    "message": "Item received at sorting office",
                },
            ],
        }
    }

    # TEST 1: Verify tracking-ID lookup and retrieval.
    with patch(
        "tracking.tracking_service.find_tracking_id",
        return_value=test_tracking_id,
    ) as mock_find:

        with patch(
            "tracking.tracking_service.get_tracking",
            return_value=simulated_response,
        ) as mock_get:

            tracking_data = track_shipment(
                test_consignment_number
            )

    assert tracking_data is not None, (
        "Tracking service did not return normalized data"
    )

    mock_find.assert_called_once_with(test_consignment_number)
    mock_get.assert_called_once_with(test_tracking_id)

    print("\nShipment Tracking Successful!\n")

    print("Consignment Number:", tracking_data["tracking_number"])
    print("Carrier:", tracking_data["carrier"])
    print("Current Status:", tracking_data["current_status"])
    print("Status Message:", tracking_data["status_message"])
    print("Origin:", tracking_data["origin"])
    print("Destination:", tracking_data["destination"])
    print("Pickup Date:", tracking_data["pickup_date"])
    print("Delivery Date:", tracking_data["delivery_date"])

    checkpoints = tracking_data["checkpoints"]

    print("\nTotal Checkpoints:", len(checkpoints))
    print("\nLatest Checkpoint:")

    if checkpoints:
        print(checkpoints[-1])

    # TEST 2: Verify normalized output.
    assert tracking_data["tracking_number"] == test_consignment_number
    assert tracking_data["carrier"] == "india-post"
    assert tracking_data["current_status"] == "InTransit"
    assert len(checkpoints) == 2

    # TEST 3: Verify missing tracking ID handling.
    with patch(
        "tracking.tracking_service.find_tracking_id",
        return_value=None,
    ):
        missing_id_result = track_shipment(
            test_consignment_number
        )

    assert missing_id_result is None

    # TEST 4: Verify failed tracking retrieval handling.
    with patch(
        "tracking.tracking_service.find_tracking_id",
        return_value=test_tracking_id,
    ):
        with patch(
            "tracking.tracking_service.get_tracking",
            return_value=None,
        ):
            failed_retrieval_result = track_shipment(
                test_consignment_number
            )

    assert failed_retrieval_result is None

    # TEST 5: Verify invalid response handling.
    with patch(
        "tracking.tracking_service.find_tracking_id",
        return_value=test_tracking_id,
    ):
        with patch(
            "tracking.tracking_service.get_tracking",
            return_value={"invalid": "response"},
        ):
            invalid_response_result = track_shipment(
                test_consignment_number
            )

    assert invalid_response_result is None

    print("\nTracking-ID lookup: PASSED")
    print("Tracking retrieval: PASSED")
    print("Response normalization: PASSED")
    print("Missing tracking ID handling: PASSED")
    print("Failed retrieval handling: PASSED")
    print("Invalid response handling: PASSED")

    print("\nTRACK SHIPMENT SERVICE TEST: PASSED")


if __name__ == "__main__":
    run_test()
