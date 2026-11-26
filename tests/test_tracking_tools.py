
from unittest.mock import patch

from ai_tools.tracking_tools import track_india_post


mock_tracking_data = {
    "tracking_number": "TEST123456789",
    "carrier": "india-post",
    "current_status": "In Transit",
    "status_message": "Shipment is in transit",
    "origin": "DELHI",
    "destination": "BENGALURU",
    "pickup_date": "2026-11-10T10:00:00+05:30",
    "delivery_date": None,
    "checkpoints": [
        {
            "date": "2026-11-10T10:00:00+05:30",
            "location": "DELHI",
            "status": "Picked Up",
            "message": "Shipment picked up"
        },
        {
            "date": "2026-11-11T15:30:00+05:30",
            "location": "BENGALURU",
            "status": "In Transit",
            "message": "Shipment in transit"
        }
    ]
}


mock_workflow_result = {
    "shipment_id": 1,
    "tracking_check_id": 1,
    "user_shipment_id": None,
    "tracking_data": mock_tracking_data,
    "inserted_checkpoints": 2
}


print("\nAI TRACKING TOOL TEST")


# Test 1: Guest tracking preserves the original one-argument call.
with patch(
    "ai_tools.tracking_tools.track_and_save_shipment",
    return_value=mock_workflow_result
) as mock_workflow:

    result = track_india_post("TEST123456789")

    mock_workflow.assert_called_once_with("TEST123456789")

assert result["success"] is True
assert result["data"]["tracking_number"] == "TEST123456789"
assert result["data"]["current_status"] == "In Transit"
assert len(result["data"]["checkpoints"]) == 2

print("Guest tracking: PASSED")


# Test 2: Registered-user tracking forwards trusted user identity.
with patch(
    "ai_tools.tracking_tools.track_and_save_shipment",
    return_value=mock_workflow_result
) as mock_workflow:

    result = track_india_post(
        "TEST123456789",
        user_id=123
    )

    mock_workflow.assert_called_once_with(
        "TEST123456789",
        user_id=123
    )

assert result["success"] is True
assert result["data"]["current_status"] == "In Transit"

print("Registered-user tracking context: PASSED")


# Test 3: Invalid user IDs must not reach the workflow.
invalid_user_ids = [
    0,
    -1,
    "123",
    True,
    False,
    1.5
]

with patch(
    "ai_tools.tracking_tools.track_and_save_shipment"
) as mock_workflow:

    for invalid_user_id in invalid_user_ids:
        result = track_india_post(
            "TEST123456789",
            user_id=invalid_user_id
        )

        assert result["success"] is False
        assert "error" in result

    mock_workflow.assert_not_called()

print("Invalid user identity rejection: PASSED")


# Test 4: Invalid consignment numbers must not call the workflow.
with patch(
    "ai_tools.tracking_tools.track_and_save_shipment"
) as mock_workflow:

    invalid_result = track_india_post("INVALID")

    assert invalid_result["success"] is False
    mock_workflow.assert_not_called()

print("Invalid consignment validation: PASSED")


# Test 5: A failed workflow must return a safe failure response.
with patch(
    "ai_tools.tracking_tools.track_and_save_shipment",
    return_value=None
):

    failed_result = track_india_post("TEST123456789")

assert failed_result["success"] is False
assert "error" in failed_result

print("Workflow failure handling: PASSED")


# Test 6: Internal database IDs must not be exposed to the AI.
with patch(
    "ai_tools.tracking_tools.track_and_save_shipment",
    return_value=mock_workflow_result
):

    result = track_india_post("TEST123456789")

assert result["success"] is True
assert "shipment_id" not in result
assert "tracking_check_id" not in result
assert "user_shipment_id" not in result

print("Internal database ID protection: PASSED")


print("\nAI TRACKING TOOL TEST: ALL TESTS PASSED")
