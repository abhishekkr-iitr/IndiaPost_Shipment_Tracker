import json

from ai_tools.tracking_tool_schema import TRACKING_TOOL_SCHEMA


print("\nTRACKING TOOL SCHEMA VERIFICATION")

# Test 1: Tool type
assert TRACKING_TOOL_SCHEMA["type"] == "function"
print("Tool Type: PASSED")

# Test 2: Function name
assert TRACKING_TOOL_SCHEMA["name"] == "track_india_post"
print("Function Name: PASSED")

# Test 3: Description
assert len(TRACKING_TOOL_SCHEMA["description"]) > 0
assert "unambiguous" in TRACKING_TOOL_SCHEMA["description"]
assert "ask the user to clarify" in TRACKING_TOOL_SCHEMA["description"]
print("Function Description and Clarification Guidance: PASSED")

# Test 4: Required parameter
parameters = TRACKING_TOOL_SCHEMA["parameters"]

assert "consignment_number" in parameters["properties"]
assert "consignment_number" in parameters["required"]
print("Required Parameter: PASSED")

# Test 5: Parameter type
assert (
    parameters["properties"]["consignment_number"]["type"]
    == "string"
)
assert "Do not" in parameters["properties"]["consignment_number"]["description"]
print("Parameter Type and Candidate-Selection Guidance: PASSED")

# Test 6: Strict mode
assert TRACKING_TOOL_SCHEMA["strict"] is True
print("Strict Mode: PASSED")

# Test 7: JSON serialization
json_schema = json.dumps(TRACKING_TOOL_SCHEMA)
assert json.loads(json_schema) == TRACKING_TOOL_SCHEMA
print("JSON Serialization: PASSED")

print("\nTRACKING TOOL SCHEMA TEST: PASSED")