import logging
import json
import math
import os
import re
from datetime import datetime, date
from zoneinfo import ZoneInfo
logger = logging.getLogger(__name__)

from openai import OpenAI

from ai_tools.assistant_config import (
    resolve_instructions,
    resolve_tool_schemas,
    resolve_config_identity,
)
from ai_tools.tool_executor import execute_tool
from ai_tools.consignment_number_parser import (
    build_consignment_validation_context,
    format_consignment_validation_instructions,
)
from database.conversation_repository import (
    add_message,
    get_conversation,
    get_conversation_messages,
)

from database.conversation_request_repository import (
    create_request,
    get_request,
    complete_request,
    fail_request,
    interrupt_request,
)

from database.conversation_tool_call_repository import (
    start_tool_execution,
    complete_tool_execution,
    interrupt_tool_execution,
)

from database.conversation_tool_round_repository import (
    create_tool_round,
    start_tool_round,
    complete_tool_round,
    set_tool_round_status,
)

DEFAULT_MAX_TOOL_ROUNDS = 3
DEFAULT_MAX_HISTORY_CHARS = 24000


def _get_max_history_chars():
    """Read and validate the configured model-input history budget."""
    raw_value = os.getenv(
        "OPENAI_MAX_HISTORY_CHARS",
        str(DEFAULT_MAX_HISTORY_CHARS),
    )

    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        raise OpenAIAgentError(
            "OPENAI_MAX_HISTORY_CHARS must be a positive integer."
        )

    if value <= 0:
        raise OpenAIAgentError(
            "OPENAI_MAX_HISTORY_CHARS must be a positive integer."
        )

    return value


def _message_character_cost(message):
    """Estimate a message's size using persisted text and tool identifiers."""
    fields = (
        message.get("content"),
        message.get("tool_name"),
        message.get("tool_call_id"),
    )
    return sum(len(value) for value in fields if isinstance(value, str))


def _conversation_history_units(messages):
    """Group tool calls with all matching outputs; keep each group indivisible.

    Ordinary messages are individual units. A malformed or incomplete tool
    group is marked invalid so a dangling function call/output is never sent.
    """
    units = []
    index = 0

    while index < len(messages):
        message = messages[index]
        is_call = (
            message.get("role") == "assistant"
            and message.get("tool_name")
            and message.get("tool_call_id")
        )

        if not is_call:
            if message.get("role") == "tool":
                units.append(([], False))
            else:
                units.append(([message], True))
            index += 1
            continue

        calls = []
        while index < len(messages):
            candidate = messages[index]
            if not (
                candidate.get("role") == "assistant"
                and candidate.get("tool_name")
                and candidate.get("tool_call_id")
            ):
                break
            calls.append(candidate)
            index += 1

        outputs = []
        while index < len(messages) and messages[index].get("role") == "tool":
            outputs.append(messages[index])
            index += 1

        call_ids = [item.get("tool_call_id") for item in calls]
        output_ids = [item.get("tool_call_id") for item in outputs]
        complete = (
            len(calls) == len(outputs)
            and len(call_ids) == len(set(call_ids))
            and set(call_ids) == set(output_ids)
            and len(output_ids) == len(set(output_ids))
        )
        units.append((calls + outputs if complete else [], complete))

    return units


def _select_bounded_conversation_history(messages, max_chars):
    """Select the newest complete history units within a character budget.

    The newest user message is always retained intact, even when it alone
    exceeds the budget. The returned messages remain in chronological order.
    """
    if not messages:
        return []

    units = _conversation_history_units(messages)
    selected_reversed = []
    used_chars = 0
    newest_user_message = next(
        (item for item in reversed(messages) if item.get("role") == "user"),
        None,
    )

    for unit, valid in reversed(units):
        if not valid:
            break

        unit_cost = sum(_message_character_cost(item) for item in unit)
        contains_current_user = (
            newest_user_message is not None
            and any(item is newest_user_message for item in unit)
        )

        if contains_current_user and not selected_reversed:
            selected_reversed.extend(reversed(unit))
            used_chars += unit_cost
            if used_chars > max_chars:
                break
            continue

        if used_chars + unit_cost > max_chars:
            break

        selected_reversed.extend(reversed(unit))
        used_chars += unit_cost

    selected = list(reversed(selected_reversed))

    # Defensive guarantee: never omit the latest user message.
    if newest_user_message is not None and newest_user_message not in selected:
        selected.append(newest_user_message)

    return selected


class OpenAIAgentError(Exception):
    """Raised when the OpenAI agent cannot complete a request."""


def _get_openai_client():
    """Create a portable OpenAI client using environment configuration."""
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        raise OpenAIAgentError("OpenAI API key is not configured.")

    try:
        timeout = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "30"))
        max_retries = int(os.getenv("OPENAI_MAX_RETRIES", "2"))

        if not math.isfinite(timeout) or timeout <= 0 or max_retries < 0:
            raise ValueError

    except ValueError:
        raise OpenAIAgentError(
            "Invalid OpenAI timeout or retry configuration."
        )

    return OpenAI(
        api_key=api_key,
        timeout=timeout,
        max_retries=max_retries
    )


def _validate_tracking_call_candidate(function_call_arguments, validation_context):
    """Reject model tracking calls that do not match one deterministic candidate.

    This gate does not decide tracking intent. It only enforces that a requested
    tracking call uses the single unambiguous candidate extracted from user
    messages. The existing executor remains responsible for authorization and
    its own argument validation.
    """
    try:
        arguments = json.loads(function_call_arguments)
    except (TypeError, ValueError):
        return False, "The tracking request contained invalid arguments."

    if not isinstance(arguments, dict):
        return False, "The tracking request contained invalid arguments."

    requested = arguments.get("consignment_number")
    if not isinstance(requested, str):
        return False, "A valid consignment number was not supplied."

    requested = requested.strip().upper()
    current_candidates = validation_context["current_candidates"]
    historical_candidates = validation_context["historical_candidates"]

    if current_candidates:
        if (
            len(current_candidates) != 1
            or len(validation_context["current_valid"]) != 1
            or validation_context["current_invalid"]
        ):
            return False, "Multiple or invalid consignment-number candidates require clarification."
        expected = validation_context["current_valid"][0]
    else:
        # A prior number is eligible only when the entire user-authored
        # history contains exactly one candidate and it has valid length.
        if (
            len(historical_candidates) != 1
            or len(validation_context["historical_valid"]) != 1
        ):
            return False, "No single unambiguous consignment number is available."
        expected = validation_context["historical_valid"][0]

    if len(requested) != 13 or not requested.isalnum() or requested != expected:
        return False, "The requested consignment number does not match the single validated user-supplied candidate."

    return True, None


def _format_tracking_freshness_instructions(current_date=None):
    """Give the model an authoritative India-local date for age comparisons."""
    if current_date is None:
        current_date = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    if not hasattr(current_date, "isoformat"):
        raise TypeError("current_date must be a date-like value.")
    return (
        "\n\nTRACKING FRESHNESS DATE CONTEXT (PYTHON):\n"
        f"Current India-local date: {current_date.isoformat()}.\n"
        "Compare the latest checkpoint's recorded date with this date. "
        "If it is materially old (including a checkpoint 13 days old), "
        "explicitly say that the update is old/stale, state the checkpoint "
        "date, and explain that it may not reflect the shipment's current "
        "location or status. Do not describe an old checkpoint as current."
    )


def _india_local_date():
    """Return the current date in the application's India-local timezone."""
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def _format_stale_checkpoint_disclosure(
    tool_result,
    current_date=None,
    minimum_age_days=13,
):
    """Build a complete, deterministic response for an old checkpoint.

    A stale result must not be presented with a model-authored current-status
    claim. The returned text therefore leads with the last recorded checkpoint
    and explicitly says that it does not establish the shipment's current
    location or status.
    """
    if not isinstance(tool_result, dict) or tool_result.get("success") is not True:
        return None

    data = tool_result.get("data")
    if not isinstance(data, dict):
        return None

    checkpoints = data.get("checkpoints")
    if not isinstance(checkpoints, list) or not checkpoints:
        return None

    dated_checkpoints = []
    for checkpoint in checkpoints:
        if not isinstance(checkpoint, dict):
            continue
        raw_date = checkpoint.get("date")
        if not isinstance(raw_date, str) or not raw_date.strip():
            continue
        try:
            parsed = datetime.fromisoformat(raw_date.strip().replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.combine(date.fromisoformat(raw_date.strip()), datetime.min.time())
            except ValueError:
                continue
        dated_checkpoints.append((parsed.date(), checkpoint))

    if not dated_checkpoints:
        return None

    latest_checkpoint_date = max(item[0] for item in dated_checkpoints)
    latest_checkpoint = next(
        checkpoint for checkpoint_date, checkpoint in dated_checkpoints
        if checkpoint_date == latest_checkpoint_date
    )
    reference_date = current_date or _india_local_date()
    if not isinstance(reference_date, date):
        raise TypeError("current_date must be a date.")

    age_days = (reference_date - latest_checkpoint_date).days
    if age_days < minimum_age_days:
        return None

    recorded_date = latest_checkpoint_date.strftime("%d %B %Y").lstrip("0")
    status = latest_checkpoint.get("status")
    location = latest_checkpoint.get("location")

    if isinstance(status, str) and status.strip():
        normalized_status = status.strip()
        display_status = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", normalized_status)
        display_status = display_status.replace("_", " ")
        checkpoint_summary = f"The latest recorded checkpoint shows {display_status}"
    else:
        checkpoint_summary = "The latest recorded checkpoint"

    if isinstance(location, str) and location.strip():
        checkpoint_summary += f" at {location.strip()}"
    checkpoint_summary += f" on {recorded_date}."

    return (
        checkpoint_summary
        + f" This update is {age_days} days old and may be stale. "
        + "It describes the last recorded checkpoint only; it does not "
        + "confirm the shipment's current location or current status."
    )


def _append_required_freshness_disclosures(response_text, disclosures):
    """Append deterministic freshness disclosures unless already complete."""
    result = response_text
    for disclosure in disclosures:
        date_text = disclosure.split(" is dated ", 1)[-1].split(" (", 1)[0]
        lower_result = result.lower()
        has_date = date_text.lower() in lower_result
        has_warning = any(
            phrase in lower_result
            for phrase in ("may be stale", "may not reflect", "out of date")
        )
        if has_date and has_warning:
            continue
        if result and not result.endswith("\\n"):
            result += "\\n\\n"
        result += disclosure
    return result



def _normalize_tracking_status(value):
    """Normalize comparable status labels without guessing from prose."""
    if not isinstance(value, str) or not value.strip():
        return None
    compact = re.sub(r"[^a-z0-9]", "", value.lower())
    aliases = {
        "intransit": "in_transit",
        "intransititem": "in_transit",
        "transit": "in_transit",
        "delivered": "delivered",
        "delivery": "delivered",
        "outfordelivery": "out_for_delivery",
        "outfordel": "out_for_delivery",
        "exception": "exception",
        "deliveryexception": "exception",
        "undelivered": "undelivered",
        "returned": "returned",
        "returnedtosender": "returned",
    }
    return aliases.get(compact, compact)


def _format_tracking_conflict_disclosure(tool_result):
    """Return a deterministic note when structured status fields disagree."""
    if not isinstance(tool_result, dict) or tool_result.get("success") is not True:
        return None
    data = tool_result.get("data")
    if not isinstance(data, dict):
        return None

    current_status = data.get("current_status", data.get("status"))
    current_key = _normalize_tracking_status(current_status)
    if not current_key:
        return None

    checkpoints = data.get("checkpoints")
    if not isinstance(checkpoints, list):
        return None

    conflicts = []
    for checkpoint in checkpoints:
        if not isinstance(checkpoint, dict):
            continue
        checkpoint_status = checkpoint.get("status")
        checkpoint_key = _normalize_tracking_status(checkpoint_status)
        # Earlier lifecycle checkpoints commonly differ from the current
        # status (for example, Picked Up followed by In Transit). Treat only
        # a Delivered checkpoint paired with a non-Delivered current-status
        # field as a direct contradiction; do not label normal progression
        # as a conflict.
        if checkpoint_key == "delivered" and current_key != "delivered":
            conflicts.append((checkpoint_status, checkpoint))

    if not conflicts:
        return None

    # Report the most recent conflicting checkpoint when dates are available;
    # otherwise preserve the first structured conflict returned by the provider.
    def checkpoint_sort_key(item):
        raw = item[1].get("date")
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(
                    raw.strip().replace("Z", "+00:00")
                ).timestamp()
            except (ValueError, OverflowError):
                pass
        return float("-inf")

    checkpoint_status, checkpoint = max(conflicts, key=checkpoint_sort_key)
    details = (
        f"The current-status field reports '{current_status}', while a "
        f"checkpoint reports '{checkpoint_status}'"
    )
    raw_date = checkpoint.get("date")
    if isinstance(raw_date, str) and raw_date.strip():
        try:
            parsed = datetime.fromisoformat(raw_date.strip().replace("Z", "+00:00"))
            details += f" on {parsed.strftime('%d %B %Y').lstrip('0')}"
        except ValueError:
            details += f" with recorded date {raw_date.strip()}"
    location = checkpoint.get("location")
    if isinstance(location, str) and location.strip():
        details += f" at {location.strip()}"
    return (
        "Data consistency note: "
        + details
        + ". These records conflict, and the available data does not establish "
          "which value is authoritative. I cannot confirm the shipment's "
          "current status from this response."
    )


def _format_unavailable_tracking_disclosure(tool_result):
    """Distinguish an empty tracking result from invalid input/provider failure."""
    if not isinstance(tool_result, dict) or tool_result.get("success") is not False:
        return None
    error = tool_result.get("error")
    if not isinstance(error, str):
        return None
    normalized = re.sub(r"\s+", " ", error.strip().lower())
    unavailable_messages = {
        "no tracking information available.",
        "no tracking information available",
        "tracking information was not available.",
        "tracking information was not available",
        "no tracking data available.",
        "no tracking data available",
    }
    if normalized not in unavailable_messages:
        return None
    return (
        "Availability note: The tracking lookup returned no tracking "
        "information. This does not establish that the consignment number "
        "is invalid, that the shipment is lost, or that no movement has "
        "occurred. The available result does not establish the shipment's "
        "current status."
    )



def _format_unknown_status_disclosure(tool_result):
    """Return a deterministic qualification for an unknown/unrecognized status.

    Any checkpoint included is described only as a recorded event, never as
    proof of the shipment's current location or status.
    """
    if not isinstance(tool_result, dict) or tool_result.get("success") is not True:
        return None

    data = tool_result.get("data")
    if not isinstance(data, dict):
        return None

    raw_status = data.get("current_status", data.get("status"))
    normalized_status = _normalize_tracking_status(raw_status)
    if not normalized_status:
        return None

    recognized_statuses = {
        "in_transit", "delivered", "out_for_delivery", "exception",
        "undelivered", "returned",
    }
    if normalized_status in recognized_statuses:
        return None

    display_status = raw_status.strip() if isinstance(raw_status, str) else str(raw_status)
    parts = [
        "Unknown-status note: "
        f"The tracking result reports an unknown or unrecognized status "
        f"('{display_status}')."
    ]

    status_message = data.get("status_message")
    if isinstance(status_message, str) and status_message.strip():
        parts.append(f"The recorded status message is: {status_message.strip()}.")

    checkpoints = data.get("checkpoints")
    usable_checkpoints = []
    if isinstance(checkpoints, list):
        for checkpoint in checkpoints:
            if not isinstance(checkpoint, dict):
                continue
            if any(
                isinstance(checkpoint.get(field), str)
                and checkpoint[field].strip()
                for field in ("date", "location", "status", "message")
            ):
                usable_checkpoints.append(checkpoint)

    if usable_checkpoints:
        def checkpoint_sort_key(checkpoint):
            raw_date = checkpoint.get("date")
            if isinstance(raw_date, str) and raw_date.strip():
                try:
                    parsed = datetime.fromisoformat(
                        raw_date.strip().replace("Z", "+00:00")
                    )
                    return parsed.timestamp()
                except (ValueError, OverflowError):
                    try:
                        return datetime.combine(
                            date.fromisoformat(raw_date.strip()),
                            datetime.min.time(),
                        ).timestamp()
                    except ValueError:
                        pass
            return float("-inf")

        checkpoint = max(usable_checkpoints, key=checkpoint_sort_key)
        details = []
        checkpoint_status = checkpoint.get("status")
        if isinstance(checkpoint_status, str) and checkpoint_status.strip():
            details.append(f"status {checkpoint_status.strip()}")
        location = checkpoint.get("location")
        if isinstance(location, str) and location.strip():
            details.append(f"location {location.strip()}")
        raw_date = checkpoint.get("date")
        if isinstance(raw_date, str) and raw_date.strip():
            try:
                parsed = datetime.fromisoformat(raw_date.strip().replace("Z", "+00:00"))
                details.append(f"recorded on {parsed.strftime('%d %B %Y').lstrip('0')}")
            except ValueError:
                details.append(f"recorded date {raw_date.strip()}")
        message = checkpoint.get("message")
        if isinstance(message, str) and message.strip():
            details.append(f"message '{message.strip()}'")
        parts.append(
            "The latest available checkpoint records "
            + ", ".join(details)
            + ". This is a recorded checkpoint only."
        )
    elif not (isinstance(status_message, str) and status_message.strip()):
        parts.append(
            "No usable status message or checkpoint details are available "
            "in this result."
        )

    parts.append(
        "The available records do not establish the shipment's current status. "
        "This does not establish that the consignment is unregistered, lost, "
        "or has not moved."
    )
    return " ".join(parts)


def _append_required_tracking_disclosures(response_text, disclosures):
    """Append mandatory structured-data qualifications not present in answer."""
    result = response_text
    for disclosure in disclosures:
        lower_result = result.lower()
        if disclosure.lower() in lower_result:
            continue
        if result and not result.endswith("\n"):
            result += "\n\n"
        result += disclosure
    return result


def _clarification_for_ambiguous_tracking(validation_context):
    """Build a deterministic clarification when a model call is rejected."""
    current_candidates = validation_context.get("current_candidates", [])
    current_invalid = validation_context.get("current_invalid", [])
    historical_candidates = validation_context.get("historical_candidates", [])

    if len(current_candidates) > 1:
        return (
            "I found more than one possible consignment number in your "
            "message. Which one would you like me to track?"
        )
    if current_invalid:
        return (
            "The consignment number provided does not appear to be exactly "
            "13 alphanumeric characters. Please check it and send the "
            "correct number."
        )
    if not current_candidates and len(historical_candidates) > 1:
        return (
            "I found more than one possible consignment number in the "
            "conversation. Which shipment do you mean?"
        )
    if len(current_candidates) == 1 and len(validation_context.get("current_valid", [])) == 1:
        return (
            "I could not safely match the requested tracking call to the "
            "consignment number you supplied. Please confirm the number "
            "you want tracked."
        )
    if not current_candidates and len(validation_context.get("historical_valid", [])) == 1:
        return (
            "I could not safely confirm which previous shipment you mean. "
            "Please clarify which consignment you want me to track."
        )
    return (
        "I cannot identify one unambiguous valid consignment number. "
        "Please clarify which shipment you want me to track."
    )


def _tools_for_consignment_context(tools, validation_context):
    """Withhold tracking capability when Python detects ambiguous candidates."""
    if not isinstance(tools, list) or not isinstance(validation_context, dict):
        return tools

    current_candidates = validation_context.get("current_candidates", [])
    current_invalid = validation_context.get("current_invalid", [])
    historical_candidates = validation_context.get(
        "historical_candidates", []
    )
    ambiguous_current = len(current_candidates) > 1 or bool(current_invalid)
    ambiguous_history = not current_candidates and len(historical_candidates) > 1

    if not (ambiguous_current or ambiguous_history):
        return tools

    return [
        tool for tool in tools
        if not (
            isinstance(tool, dict)
            and tool.get("type") == "function"
            and tool.get("name") == "track_india_post"
        )
    ]


def _serialize_tool_result(result):
    """Convert a tool result into JSON suitable for the Responses API."""
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps({
            "success": False,
            "error": "The tool returned an unserializable result."
        })


def _build_responses_input(messages):
    """
    Rebuild Responses API input items from persisted conversation messages.

    Stored assistant function calls and tool outputs are reconstructed as
    their corresponding Responses API item types. Hidden model reasoning is
    never stored or reconstructed.
    """
    response_input = []

    for message in messages:
        role = message["role"]
        content = message["content"]
        tool_name = message.get("tool_name")
        tool_call_id = message.get("tool_call_id")

        if role == "assistant" and tool_name and tool_call_id:
            response_input.append({
                "type": "function_call",
                "name": tool_name,
                "call_id": tool_call_id,
                "arguments": content
            })

        elif role == "tool" and tool_call_id:
            response_input.append({
                "type": "function_call_output",
                "call_id": tool_call_id,
                "output": content
            })

        else:
            response_input.append({
                "role": role,
                "content": content
            })

    return response_input


def _store_tool_call(conversation_id, function_call, user_id):
    """Persist a function call as an assistant message."""
    add_message(
        conversation_id=conversation_id,
        role="assistant",
        content=function_call.arguments,
        user_id=user_id,
        tool_name=function_call.name,
        tool_call_id=function_call.call_id
    )


def _store_tool_output(conversation_id, function_call, output, user_id):
    """Persist a function result as a tool message."""
    add_message(
        conversation_id=conversation_id,
        role="tool",
        content=output,
        user_id=user_id,
        tool_name=function_call.name,
        tool_call_id=function_call.call_id
    )



def _complete_tool_execution(
    tool_execution_id,
    output,
    user_id,
):
    """Persist a tool output and mark its execution completed atomically."""

    return complete_tool_execution(
        tool_execution_id=tool_execution_id,
        output_content=output,
        user_id=user_id,
    )


def ask_openai(
    user_message,
    client=None,
    model=None,
    max_tool_rounds=DEFAULT_MAX_TOOL_ROUNDS,
    user_context=None,
    conversation_id=None,
    request_id=None,
    assistant_config=None,
    tool_executor=None,
    conversation_context_messages=None,
):
    """
    Send a user message to OpenAI and execute approved function calls.

    When conversation_id is omitted, the original stateless behavior is
    preserved. When supplied, the conversation must already exist and must
    belong to the trusted user context (or be a guest conversation).

    Args:
        user_message (str): User's message.
        client: Optional OpenAI-compatible client, useful for mocked tests.
        model (str): OpenAI model name. If omitted, OPENAI_MODEL is used.
        max_tool_rounds (int): Maximum number of tool-execution rounds.
        user_context (dict, optional): Trusted application context.
            This is separate from the user's message and AI tool arguments.
        conversation_id (str, optional): Existing conversation UUID.

    Returns:
        str: The model's final text response.

    Raises:
        OpenAIAgentError: If configuration, API interaction, or execution fails.
    """
    if not isinstance(user_message, str) or not user_message.strip():
        raise ValueError("A non-empty user message must be provided.")

    if (
        not isinstance(max_tool_rounds, int)
        or isinstance(max_tool_rounds, bool)
        or max_tool_rounds < 1
    ):
        raise ValueError("max_tool_rounds must be a positive integer.")

    if user_context is not None:
        if (
            not isinstance(user_context, dict)
            or set(user_context.keys()) != {"user_id"}
            or not isinstance(user_context["user_id"], int)
            or isinstance(user_context["user_id"], bool)
            or user_context["user_id"] <= 0
        ):
            raise ValueError("Invalid application user context.")

    if conversation_id is not None:
        if (
            not isinstance(conversation_id, str)
            or not conversation_id.strip()
        ):
            raise ValueError("conversation_id must be a non-empty string.")

    if conversation_context_messages is not None:
        if conversation_id is not None:
            raise ValueError(
                "conversation_context_messages is only supported for "
                "stateless evaluation requests."
            )
        if not isinstance(conversation_context_messages, list):
            raise TypeError("conversation_context_messages must be a list.")
        for message in conversation_context_messages:
            if (
                not isinstance(message, dict)
                or message.get("role") not in {"user", "assistant", "tool"}
                or not isinstance(message.get("content"), str)
            ):
                raise ValueError(
                    "Each conversation context message must contain a "
                    "supported role and string content."
                )

    if request_id is not None:
        if (
            not isinstance(request_id, str)
            or not request_id.strip()
        ):
            raise ValueError("request_id must be a non-empty string.")

        if conversation_id is None:
            raise ValueError(
                "request_id requires an existing conversation_id."
            )

    # Resolve the trusted executor dependency. Existing production callers
    # continue to use the imported production executor by default.
    selected_tool_executor = (
        execute_tool if tool_executor is None else tool_executor
    )
    if not callable(selected_tool_executor):
        raise TypeError("tool_executor must be callable.")

    # Resolve trusted application configuration before making any API call.
    # Configuration is optional so existing callers retain their behaviour.
    resolved_instructions = resolve_instructions(assistant_config)
    resolved_tools = resolve_tool_schemas(assistant_config)
    config_identity = resolve_config_identity(assistant_config)

    # Log identifiers and a content fingerprint only; never log instructions
    # or the configuration body itself.
    logger.info(
        "Assistant configuration applied: config_id=%s config_version=%s fingerprint=%s",
        config_identity["config_id"],
        config_identity["config_version"],
        config_identity["fingerprint"],
    )

    selected_model = model or os.getenv("OPENAI_MODEL")

    if not selected_model:
        raise OpenAIAgentError("OpenAI model is not configured.")

    if client is None:
        client = _get_openai_client()

    try:
        request_created = False
        request_interrupted = False
        stored_messages = None
        user_id = user_context["user_id"] if user_context else None

        bounded_messages = []
        if conversation_id is not None:
            conversation = get_conversation(
                conversation_id,
                user_id=user_id
            )

            if not conversation or conversation.get("status") != "active":
                raise OpenAIAgentError(
                    "The conversation is unavailable or is not active."
                )

            if request_id is not None:
                request_record = create_request(
                    request_id=request_id,
                    conversation_id=conversation_id,
                    user_message=user_message,
                    user_id=user_id
                )

                if not request_record["created"]:
                    request_status = request_record["status"]

                    if request_status == "completed":
                        existing_messages = get_conversation_messages(
                            conversation_id,
                            user_id=user_id
                        )

                        response_message = next(
                            (
                                message
                                for message in existing_messages
                                if message["message_id"]
                                == request_record["response_message_id"]
                            ),
                            None
                        )

                        if response_message is None:
                            raise OpenAIAgentError(
                                "The stored response for this request "
                                "could not be found."
                            )

                        return response_message["content"]

                    if request_status == "processing":
                        raise OpenAIAgentError(
                            "This request is already being processed. "
                            "Please check its status before retrying."
                        )

                    if request_status == "failed":
                        raise OpenAIAgentError(
                            "This request previously failed. "
                            "It requires controlled recovery."
                        )

                    if request_status == "interrupted":
                        raise OpenAIAgentError(
                            "This request was interrupted. "
                            "It requires recovery assessment."
                        )

                    raise OpenAIAgentError(
                        "The request has an unrecognized status."
                    )

                request_created = True

            else:
                # Preserve the existing behavior for callers without
                # a request ID.
                add_message(
                    conversation_id=conversation_id,
                    role="user",
                    content=user_message,
                    user_id=user_id
                )

            stored_messages = get_conversation_messages(
                conversation_id,
                user_id=user_id
            )
            history_budget = _get_max_history_chars()
            bounded_messages = _select_bounded_conversation_history(
                stored_messages,
                history_budget,
            )
            request_input = _build_responses_input(bounded_messages)

        else:
            # Preserve the original stateless behavior unless the evaluation
            # harness supplies structured prior messages. In that case, keep
            # the current user message separate and render the prior context
            # only for model input; candidate extraction still uses roles.
            request_input = user_message
            if conversation_context_messages:
                context_lines = ["Previous conversation:"]
                for context_message in conversation_context_messages:
                    role = context_message["role"].capitalize()
                    content = context_message["content"]
                    context_lines.append(f"{role}: {content}")
                context_lines.extend([
                    "",
                    "Current user message:",
                    user_message,
                ])
                request_input = "\n".join(context_lines)

        # Deterministically identify candidate consignment numbers from
        # user-authored messages only. This supplements, but does not replace,
        # model intent recognition or the authoritative Python executor.
        candidate_history = list(bounded_messages)
        if conversation_context_messages:
            candidate_history.extend(conversation_context_messages)

        validation_context = build_consignment_validation_context(
            candidate_history,
            user_message,
        )
        validation_instructions = format_consignment_validation_instructions(
            validation_context
        )
        request_instructions = (
            resolved_instructions
            + _format_tracking_freshness_instructions()
        )
        if validation_instructions:
            request_instructions += validation_instructions

        request_tools = _tools_for_consignment_context(
            resolved_tools,
            validation_context,
        )

        response = client.responses.create(
            model=selected_model,
            input=request_input,
            instructions=request_instructions,
            tools=request_tools
        )

        tool_rounds = 0
        required_tracking_disclosures = []
        deterministic_clarification = None

        while True:
            function_calls = [
                item for item in response.output
                if item.type == "function_call"
            ]

            if not function_calls:
                final_text = response.output_text

                if not final_text:
                    raise OpenAIAgentError(
                        "The model returned no final text response."
                    )

                # Conflict and unavailable-data results are application-level
                # response overrides, not suffixes. Returning only the
                # deterministic disclosure prevents a speculative or
                # overconfident model opening from being shown to the user.
                priority_disclosures = [
                    item for item in required_tracking_disclosures
                    if item.startswith((
                        "Data consistency note:",
                        "Availability note:",
                        "Unknown-status note:",
                    ))
                ]
                stale_disclosures = [
                    item for item in required_tracking_disclosures
                    if item.startswith("The latest recorded checkpoint")
                ]
                if priority_disclosures:
                    final_text = "\n\n".join(priority_disclosures)
                    if stale_disclosures:
                        # Preserve conflict/unavailability priority while also
                        # disclosing that the latest checkpoint may be stale.
                        final_text += "\n\n" + "\n\n".join(stale_disclosures)
                elif stale_disclosures:
                    # A stale checkpoint cannot support a present-tense
                    # current-status claim. Replace the model's complete
                    # answer with the deterministic last-recorded update.
                    final_text = "\n\n".join(stale_disclosures)
                else:
                    final_text = _append_required_tracking_disclosures(
                        final_text, required_tracking_disclosures
                    )

                if conversation_id is not None:
                    assistant_message = add_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=final_text,
                        user_id=user_id
                    )

                    if request_created:
                        complete_request(
                            request_id=request_id,
                            response_message_id=assistant_message["message_id"],
                            user_id=user_id
                        )

                return final_text


            if tool_rounds >= max_tool_rounds:
                raise OpenAIAgentError(
                    "Maximum tool-execution rounds exceeded."
                )


            tool_outputs = []
            execution_records = {}
            active_round_id = None

            # Persist the complete group of function calls atomically
            # before executing any individual tool.
            if conversation_id is not None:

                tool_call_records = [
                    {
                        "tool_call_id": function_call.call_id,
                        "tool_name": function_call.name,
                        "arguments": function_call.arguments,
                        "call_content": function_call.arguments,
                        "provider_message_id": getattr(
                            function_call, "id", None
                        ),
                    }
                    for function_call in function_calls
                ]

                round_record = create_tool_round(
                    conversation_id=conversation_id,
                    round_number=tool_rounds + 1,
                    tool_calls=tool_call_records,
                    request_id=request_id,
                    user_id=user_id,
                )

                active_round_id = round_record["round_id"]

                execution_records = {
                    item["tool_call_id"]: item
                    for item in round_record["executions"]
                }

                start_tool_round(
                    active_round_id,
                    user_id=user_id,
                )

            # Execute calls in their original order.
            for function_call in function_calls:

                execution_record = execution_records.get(
                    function_call.call_id
                )

                if execution_record is not None:
                    start_tool_execution(
                        execution_record["tool_execution_id"],
                        user_id=user_id,
                    )

                try:
                    executor_context = {}
                    if user_context is not None:
                        executor_context["user_context"] = user_context
                    if assistant_config is not None:
                        executor_context["assistant_config"] = assistant_config

                    if function_call.name == "track_india_post":
                        candidate_allowed, rejection_message = (
                            _validate_tracking_call_candidate(
                                function_call.arguments,
                                validation_context,
                            )
                        )
                        if not candidate_allowed:
                            result = {
                                "success": False,
                                "error": rejection_message,
                                "error_code": "consignment_candidate_gate_rejected",
                            }
                        else:
                            result = selected_tool_executor(
                                function_call.name,
                                function_call.arguments,
                                **executor_context,
                            )
                    else:
                        result = selected_tool_executor(
                            function_call.name,
                            function_call.arguments,
                            **executor_context,
                        )

                except Exception:
                    logger.exception(
                        "Unexpected error while executing tool: %s",
                        function_call.name,
                    )

                    result = {
                        "success": False,
                        "error": "Tool execution failed.",
                    }

                if (
                    function_call.name == "track_india_post"
                    and isinstance(result, dict)
                    and result.get("error_code") == "consignment_candidate_gate_rejected"
                ):
                    deterministic_clarification = (
                        _clarification_for_ambiguous_tracking(validation_context)
                    )

                if function_call.name == "track_india_post":
                    disclosures = (
                        _format_stale_checkpoint_disclosure(result),
                        _format_tracking_conflict_disclosure(result),
                        _format_unavailable_tracking_disclosure(result),
                        _format_unknown_status_disclosure(result),
                    )
                    for disclosure in disclosures:
                        if (
                            disclosure
                            and disclosure not in required_tracking_disclosures
                        ):
                            required_tracking_disclosures.append(disclosure)

                serialized_output = _serialize_tool_result(result)

                if execution_record is not None:
                    try:
                        _complete_tool_execution(
                            tool_execution_id=(
                                execution_record["tool_execution_id"]
                            ),
                            output=serialized_output,
                            user_id=user_id,
                        )

                    except Exception:
                        logger.exception(
                            "Could not persist tool output; "
                            "marking execution and round as interrupted: %s",
                            execution_record["tool_execution_id"],
                        )

                        request_interrupted = True

                        try:
                            interrupt_tool_execution(
                                execution_record["tool_execution_id"],
                                user_id=user_id,
                                error_code="output_persistence_uncertain",
                            )
                        except Exception:
                            logger.exception(
                                "Could not mark tool execution as interrupted."
                            )

                        try:
                            set_tool_round_status(
                                active_round_id,
                                "interrupted",
                                user_id=user_id,
                                error_code="output_persistence_uncertain",
                            )
                        except Exception:
                            logger.exception(
                                "Could not mark tool round as interrupted."
                            )

                        raise

                elif conversation_id is not None:
                    # Defensive fallback for an unexpected missing record.
                    _store_tool_output(
                        conversation_id,
                        function_call,
                        serialized_output,
                        user_id,
                    )

                tool_outputs.append({
                    "type": "function_call_output",
                    "call_id": function_call.call_id,
                    "output": serialized_output,
                })

            # A round is complete only after every output has been saved.
            if active_round_id is not None:
                complete_tool_round(
                    active_round_id,
                    user_id=user_id,
                )

            if deterministic_clarification is not None:
                if conversation_id is not None:
                    assistant_message = add_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=deterministic_clarification,
                        user_id=user_id,
                    )
                    if request_created:
                        complete_request(
                            request_id=request_id,
                            response_message_id=assistant_message["message_id"],
                            user_id=user_id,
                        )
                return deterministic_clarification

            tool_rounds += 1

            response = client.responses.create(
                model=selected_model,
                previous_response_id=response.id,
                input=tool_outputs,
                instructions=request_instructions,
                tools=request_tools
            )


    except OpenAIAgentError:
        if request_created:
            try:
                if request_interrupted:
                    interrupt_request(
                        request_id=request_id,
                        user_id=user_id,
                        error_code="tool_execution_interrupted"
                    )
                else:
                    fail_request(
                        request_id=request_id,
                        user_id=user_id,
                        error_code="agent_error"
                    )
            except Exception:
                logger.exception(
                    "Could not update request status: %s",
                    request_id
                )

        raise

    except Exception as exc:
        if request_created:
            try:
                if request_interrupted:
                    interrupt_request(
                        request_id=request_id,
                        user_id=user_id,
                        error_code="tool_execution_interrupted"
                    )
                else:
                    fail_request(
                        request_id=request_id,
                        user_id=user_id,
                        error_code="unexpected_error"
                    )
            except Exception:
                logger.exception(
                    "Could not update request status: %s",
                    request_id
                )

        raise OpenAIAgentError(
            "The OpenAI agent request failed. "
            "Check configuration and application logs."
        ) from exc

