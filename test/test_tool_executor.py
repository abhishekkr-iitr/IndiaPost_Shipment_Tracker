
from unittest.mock import patch

from ai_tools.tool_executor import execute_tool, AVAILABLE_TOOLS


mock_tracking_data = {
    "tracking_number": "TEST123456789",
    "carrier": "india-post",
    "current_status": "In Transit",
    "status_message": "Shipment is in transit",
    "origin": "DELHI",
    "destination": "BENGALURU",
    "pickup_date": "2026-11-10T10:00:00+05:30",
    "delivery_date": None,
    "checkpoints": []
}


def mock_tracking_tool(consignment_number, user_id=None):
    return {
        "success": True,
        "data": mock_tracking_data,
        "received_user_id": user_id
    }


print("\nTOOL EXECUTOR TESTS")


# Test 1: Successful guest tool execution.
with patch.dict(
    AVAILABLE_TOOLS,
    {"track_india_post": mock_tracking_tool}
):
    result = execute_tool(
        "track_india_post",
        {"consignment_number": "TEST123456789"}
    )

assert result["success"] is True
assert result["data"]["current_status"] == "In Transit"
assert result["received_user_id"] is None

print("Guest Tool Execution: PASSED")


# Test 2: Registered-user context is forwarded separately.
with patch.dict(
    AVAILABLE_TOOLS,
    {"track_india_post": mock_tracking_tool}
):
    result = execute_tool(
        "track_india_post",
        {"consignment_number": "TEST123456789"},
        user_context={"user_id": 123}
    )

assert result["success"] is True
assert result["received_user_id"] == 123

print("Registered-User Context Forwarding: PASSED")


# Test 3: Unauthorized tool rejection.
result = execute_tool(
    "delete_database",
    {}
)

assert result["success"] is False

print("Unauthorized Tool Rejection: PASSED")


# Test 4: Invalid JSON rejection.
result = execute_tool(
    "track_india_post",
    "{invalid json}"
)

assert result["success"] is False

print("Invalid JSON Rejection: PASSED")


# Test 5: Incorrect argument structure rejection.
result = execute_tool(
    "track_india_post",
    ["TEST123456789"]
)

assert result["success"] is False

print("Incorrect Argument Structure Rejection: PASSED")


# Test 6: Model-supplied user_id must be rejected.
result = execute_tool(
    "track_india_post",
    {
        "consignment_number": "TEST123456789",
        "user_id": 123
    }
)

assert result["success"] is False

print("Model-Supplied User ID Rejection: PASSED")


# Test 7: Missing model argument rejection.
result = execute_tool(
    "track_india_post",
    {}
)

assert result["success"] is False

print("Missing Parameter Rejection: PASSED")


# Test 8: Invalid application contexts must be rejected.
invalid_contexts = [
    "user 123",
    [],
    {"user_id": 0},
    {"user_id": -1},
    {"user_id": "123"},
    {"user_id": True},
    {"user_id": 1.5},
    {"user_id": 123, "role": "admin"}
]

with patch.dict(
    AVAILABLE_TOOLS,
    {"track_india_post": mock_tracking_tool}
):
    for context in invalid_contexts:
        result = execute_tool(
            "track_india_post",
            {"consignment_number": "TEST123456789"},
            user_context=context
        )

        assert result["success"] is False

print("Invalid Application Context Rejection: PASSED")


# Test 9: A missing application context remains valid for guests.
with patch.dict(
    AVAILABLE_TOOLS,
    {"track_india_post": mock_tracking_tool}
):
    result = execute_tool(
        "track_india_post",
        {"consignment_number": "TEST123456789"},
        user_context=None
    )

assert result["success"] is True
assert result["received_user_id"] is None

print("Guest Compatibility: PASSED")


print("\nTOOL EXECUTOR TEST: ALL TESTS PASSED")


# Test 10: Invalid tool-name types must be rejected safely.
result = execute_tool(
    ["track_india_post"],
    {"consignment_number": "TEST123456789"}
)

assert result["success"] is False

print("Invalid Tool Name Type Rejection: PASSED")




# Step 7: Python executor independently enforces guest permissions.
guest_restricted_config = {
    "tools": {
        "track_india_post": {
            "enabled": True,
            "permissions": {
                "guest": False,
                "registered_user": True
            }
        }
    }
}
with patch.dict(
    AVAILABLE_TOOLS,
    {"track_india_post": mock_tracking_tool}
):
    result = execute_tool(
        "track_india_post",
        {"consignment_number": "TEST123456789"},
        assistant_config=guest_restricted_config
    )
assert result["success"] is False
assert result["error"] == "Tool is not permitted for this user."
print("Guest Permission Enforcement: PASSED")


# Step 8: The same configuration permits a registered user.
with patch.dict(
    AVAILABLE_TOOLS,
    {"track_india_post": mock_tracking_tool}
):
    result = execute_tool(
        "track_india_post",
        {"consignment_number": "TEST123456789"},
        user_context={"user_id": 123},
        assistant_config=guest_restricted_config
    )
assert result["success"] is True
assert result["received_user_id"] == 123
print("Registered-User Permission Enforcement: PASSED")


# Step 9: Registered users can be denied independently.
registered_restricted_config = {
    "tools": {
        "track_india_post": {
            "enabled": True,
            "permissions": {
                "guest": True,
                "registered_user": False
            }
        }
    }
}
with patch.dict(
    AVAILABLE_TOOLS,
    {"track_india_post": mock_tracking_tool}
):
    result = execute_tool(
        "track_india_post",
        {"consignment_number": "TEST123456789"},
        user_context={"user_id": 123},
        assistant_config=registered_restricted_config
    )
assert result["success"] is False
print("Registered-User Denial: PASSED")


# Step 10: A disabled tool is rejected by the executor even if role flags
# are permissive.
disabled_config = {
    "tools": {
        "track_india_post": {
            "enabled": False,
            "permissions": {
                "guest": True,
                "registered_user": True
            }
        }
    }
}
with patch.dict(
    AVAILABLE_TOOLS,
    {"track_india_post": mock_tracking_tool}
):
    result = execute_tool(
        "track_india_post",
        {"consignment_number": "TEST123456789"},
        assistant_config=disabled_config
    )
assert result["success"] is False
print("Disabled-Tool Executor Enforcement: PASSED")


# Step 11: Invalid permission configuration fails closed.
invalid_config = {
    "tools": {
        "track_india_post": {
            "enabled": True,
            "permissions": {"guest": True}
        }
    }
}
with patch.dict(
    AVAILABLE_TOOLS,
    {"track_india_post": mock_tracking_tool}
):
    result = execute_tool(
        "track_india_post",
        {"consignment_number": "TEST123456789"},
        assistant_config=invalid_config
    )
assert result["success"] is False
assert result["error"] == "Invalid assistant configuration."
print("Invalid Configuration Fail-Closed: PASSED")


# Activity 10: Invalid consignment numbers must be rejected before the
# registered tool implementation is invoked.
invalid_consignment_numbers = [
    None,
    1234567890123,
    "",
    "AB123",
    "AB123456789I!",
    "AB123456789INX",
    "AB12345678 9IN",
]
for invalid_number in invalid_consignment_numbers:
    calls = []

    def should_not_run(consignment_number, user_id=None):
        calls.append(consignment_number)
        return {"success": True}

    with patch.dict(
        AVAILABLE_TOOLS,
        {"track_india_post": should_not_run}
    ):
        result = execute_tool(
            "track_india_post",
            {"consignment_number": invalid_number}
        )
    assert result["success"] is False
    assert "Invalid consignment number" in result["error"] or (
        not isinstance(invalid_number, str)
        and result["error"] == "Consignment number must be provided as text."
    )
    assert calls == []
print("Invalid Consignment Number Rejected Before Tool Execution: PASSED")

# A valid number with surrounding whitespace/lowercase remains compatible;
# the registered tool retains responsibility for canonical normalization.
with patch.dict(
    AVAILABLE_TOOLS,
    {"track_india_post": mock_tracking_tool}
):
    result = execute_tool(
        "track_india_post",
        {"consignment_number": " ab123456789in "}
    )
assert result["success"] is True
print("Valid Consignment Number Compatibility: PASSED")
