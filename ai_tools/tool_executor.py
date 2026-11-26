
import json
import logging
logger = logging.getLogger(__name__)

from ai_tools.tracking_tools import track_india_post
from ai_tools.assistant_config import is_tool_permitted


# Registry of Python functions that the AI agent is permitted to execute.
AVAILABLE_TOOLS = {
    "track_india_post": track_india_post
}


def _validate_user_context(user_context):
    """
    Validate trusted application context separately from AI arguments.

    Currently, only an internal database user_id is supported.
    Additional context fields can be introduced deliberately later.
    """

    if user_context is None:
        return True

    if not isinstance(user_context, dict):
        return False

    if set(user_context.keys()) != {"user_id"}:
        return False

    user_id = user_context["user_id"]

    if (
        not isinstance(user_id, int)
        or isinstance(user_id, bool)
        or user_id <= 0
    ):
        return False

    return True


def execute_tool(
    tool_name,
    arguments,
    user_context=None,
    assistant_config=None,
    tool_registry=None,
):
    """
    Execute an approved AI tool using model arguments and optional
    trusted application context.

    Args:
        tool_name (str): Name of the requested tool.
        arguments (dict or str): Arguments supplied by the AI model.
        user_context (dict, optional): Trusted context supplied by the
            application, never by the AI model.

    Returns:
        dict: Tool execution result.
    """

    # Step 1: Resolve a per-call registry without changing shared state.
    # The production registry remains the default for all existing callers.
    registry = AVAILABLE_TOOLS if tool_registry is None else tool_registry

    if (
        not isinstance(registry, dict)
        or not isinstance(tool_name, str)
        or tool_name not in AVAILABLE_TOOLS
        or tool_name not in registry
        or not callable(registry[tool_name])
    ):
        return {
            "success": False,
            "error": f"Unknown or unauthorized tool: {tool_name}"
        }

    # Step 2: Validate trusted application context before resolving role.
    if not _validate_user_context(user_context):
        return {
            "success": False,
            "error": "Invalid application user context."
        }

    # Step 3: Independently enforce the active trusted configuration.
    try:
        permitted = is_tool_permitted(
            tool_name,
            config=assistant_config,
            user_context=user_context,
        )
    except ValueError:
        return {
            "success": False,
            "error": "Invalid assistant configuration."
        }

    if not permitted:
        return {
            "success": False,
            "error": "Tool is not permitted for this user."
        }

    # Step 4: Parse JSON arguments if supplied as a string.
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return {
                "success": False,
                "error": "Invalid JSON arguments."
            }

    # Step 5: Validate argument structure.
    if not isinstance(arguments, dict):
        return {
            "success": False,
            "error": "Tool arguments must be a JSON object."
        }

    # Step 6: Validate expected model-supplied parameters.
    # The AI model is permitted to supply only consignment_number.
    if set(arguments.keys()) != {"consignment_number"}:
        return {
            "success": False,
            "error": "Invalid tool arguments."
        }

    # Step 7: Validate the consignment number before invoking any registered
    # implementation. This keeps malformed model arguments from reaching a
    # replacement or mocked tool implementation.
    consignment_number = arguments["consignment_number"]
    if not isinstance(consignment_number, str):
        return {
            "success": False,
            "error": "Consignment number must be provided as text."
        }

    normalized_number = consignment_number.strip().upper()
    if len(normalized_number) != 13 or not normalized_number.isalnum():
        return {
            "success": False,
            "error": (
                "Invalid consignment number. Please provide a valid "
                "13-character India Post consignment number."
            )
        }

    # Step 8: Execute the approved Python function.
    try:
        tool_function = registry[tool_name]

        # Preserve the original call for guest requests.
        if user_context is None:
            return tool_function(
                arguments["consignment_number"]
            )

        # Forward the trusted internal user ID separately.
        return tool_function(
            arguments["consignment_number"],
            user_id=user_context["user_id"]
        )

    except Exception:
        logger.exception("Approved tool execution failed.")
        return {
            "success": False,
            "error": "Tool execution failed."
        }
