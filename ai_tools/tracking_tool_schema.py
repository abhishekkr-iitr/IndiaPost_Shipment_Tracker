TRACKING_TOOL_SCHEMA = {
    "type": "function",
    "name": "track_india_post",
    "description": (
        "Retrieves the latest tracking status and complete available "
        "tracking history of an India Post shipment. Call only when the "
        "user has supplied one unambiguous, valid 13-character "
        "alphanumeric consignment number. If the number is missing, "
        "invalid, or ambiguous, ask the user to clarify instead of calling."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "consignment_number": {
                "type": "string",
                "description": (
                    "One 13-character alphanumeric India Post consignment "
                    "number explicitly provided by the user or clearly "
                    "identified in relevant conversation context. Do not "
                    "guess, complete, or choose between multiple candidates."
                )
            }
        },
        "required": ["consignment_number"],
        "additionalProperties": False
    },
    "strict": True
}

# Registry of model-facing schemas for tools implemented by the Python app.
# Keep this registry aligned with ai_tools.tool_executor.AVAILABLE_TOOLS.
TOOL_SCHEMAS = {
    "track_india_post": TRACKING_TOOL_SCHEMA,
}


def get_tool_schema(tool_name):
    """Return a defensive copy of a registered tool schema, or None."""
    from copy import deepcopy

    schema = TOOL_SCHEMAS.get(tool_name)
    return deepcopy(schema) if schema is not None else None
