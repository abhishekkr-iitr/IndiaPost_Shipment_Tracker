
from tracking.tracking_service import normalize_aftership_response


def run_test():

    # Representative AfterShip response.
    # No live API request is made.
    sample_response = {
        "data": {
            "tracking_number": "TEST123456789IN",
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

    # TEST 1: Normalize a valid response.
    normalized_data = normalize_aftership_response(
        sample_response
    )

    assert normalized_data is not None, (
        "Valid response was not normalized"
    )

    print("\nNORMALIZED TRACKING INFORMATION\n")

    for field, value in normalized_data.items():
        if field != "checkpoints":
            print(f"{field}: {value}")

    # TEST 2: Verify shipment fields.
    assert normalized_data["tracking_number"] == "TEST123456789IN"
    assert normalized_data["carrier"] == "india-post"
    assert normalized_data["current_status"] == "InTransit"
    assert (
        normalized_data["status_message"]
        == "Shipment is moving through the network"
    )
    assert normalized_data["origin"] == "Delhi, India"
    assert normalized_data["destination"] == "Bengaluru, India"
    assert (
        normalized_data["pickup_date"]
        == "2026-11-12T09:00:00+05:30"
    )
    assert normalized_data["delivery_date"] is None

    # TEST 3: Verify checkpoint normalization.
    checkpoints = normalized_data["checkpoints"]

    assert len(checkpoints) == 2, (
        f"Expected 2 checkpoints, found {len(checkpoints)}"
    )

    expected_checkpoint = {
        "date": "2026-11-12T10:00:00+05:30",
        "location": "Delhi Sorting Office",
        "status": "InTransit",
        "message": "Item dispatched",
    }

    assert checkpoints[0] == expected_checkpoint, (
        "First checkpoint was not normalized correctly"
    )

    print("\nNORMALIZED CHECKPOINTS\n")

    for checkpoint in checkpoints:
        print(checkpoint)

    # TEST 4: Invalid response types.
    assert normalize_aftership_response(None) is None
    assert normalize_aftership_response("invalid") is None
    assert normalize_aftership_response([]) is None

    # TEST 5: Missing or invalid data.
    assert normalize_aftership_response({}) is None
    assert normalize_aftership_response(
        {"data": None}
    ) is None
    assert normalize_aftership_response(
        {"data": []}
    ) is None

    # TEST 6: Invalid checkpoint list.
    assert normalize_aftership_response(
        {
            "data": {
                "tracking_number": "TEST123",
                "checkpoints": "invalid",
            }
        }
    ) is None

    # TEST 7: Non-dictionary checkpoint entries are skipped.
    mixed_checkpoint_response = {
        "data": {
            "tracking_number": "TEST123",
            "checkpoints": [
                {
                    "checkpoint_time": "2026-11-12T10:00:00+05:30",
                    "location": "Test Office",
                    "tag": "InTransit",
                    "message": "Test event",
                },
                "invalid checkpoint",
                None,
            ],
        }
    }

    mixed_result = normalize_aftership_response(
        mixed_checkpoint_response
    )

    assert mixed_result is not None
    assert len(mixed_result["checkpoints"]) == 1

    print("\nInvalid response handling: PASSED")
    print("Invalid checkpoint filtering: PASSED")

    print("\nTRACKING NORMALIZATION TEST: PASSED")


if __name__ == "__main__":
    run_test()
