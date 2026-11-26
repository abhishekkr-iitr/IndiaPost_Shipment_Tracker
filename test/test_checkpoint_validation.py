
from unittest.mock import patch


def run_test():
    consignment_number = "TEST123456789IN"

    # Simulated tracking response:
    # - Checkpoint 1 has all required fields.
    # - Checkpoint 2 has no message.
    # - Checkpoint 3 has no location field.
    simulated_tracking_data = {
        "tracking_number": consignment_number,
        "current_status": "In Transit",
        "checkpoints": [
            {
                "date": "2026-11-11T10:00:00+05:30",
                "location": "NEW DELHI, INDIA",
                "status": "In Transit",
                "message": "Shipment dispatched",
            },
            {
                "date": "2026-11-11T15:00:00+05:30",
                "location": "MUMBAI, INDIA",
                "status": "In Transit",
                "message": None,
            },
            {
                "date": "2026-11-12T09:00:00+05:30",
                "status": "Out for Delivery",
                "message": "Shipment reached delivery office",
            },
        ],
    }

    # Import the tracking service for patching.
    from tracking import tracking_service

    with patch.object(
        tracking_service,
        "track_shipment",
        return_value=simulated_tracking_data,
    ) as mock_track:

        tracking_data = tracking_service.track_shipment(
            consignment_number
        )

    mock_track.assert_called_once_with(consignment_number)

    assert tracking_data is not None

    checkpoints = tracking_data.get("checkpoints", [])

    print("\nCheckpoint Validation Results\n")
    print("Total Checkpoints:", len(checkpoints))

    valid_checkpoints = 0
    incomplete_checkpoints = 0

    for index, checkpoint in enumerate(checkpoints, start=1):

        required_fields = [
            "date",
            "location",
            "status",
            "message",
        ]

        missing_fields = [
            field for field in required_fields
            if checkpoint.get(field) is None
        ]

        if not missing_fields:
            valid_checkpoints += 1

        else:
            incomplete_checkpoints += 1

            print(
                f"Checkpoint {index} has missing fields:",
                missing_fields,
            )

    print("\nValid Checkpoints:", valid_checkpoints)
    print("Incomplete Checkpoints:", incomplete_checkpoints)

    # Verify expected validation results.
    assert valid_checkpoints == 1
    assert incomplete_checkpoints == 2

    print("\nCheckpoint validation requires attention.")
    print("\nCHECKPOINT VALIDATION TEST: PASSED")


if __name__ == "__main__":
    run_test()
