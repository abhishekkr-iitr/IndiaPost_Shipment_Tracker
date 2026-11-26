
"""
Milestone 2 - Stage 7 - Step 6
Real-model semantic evaluation infrastructure.

This module provides:
- Access to the approved 39-scenario catalogue.
- Structured evaluation records.
- A recording wrapper for Responses API clients.
- Fixture-backed tracking execution.

Importing this module does not make API calls.
No live tracking provider is contacted by the fixture provider.
"""

import copy
import json
import math
import os
import re
from types import SimpleNamespace
from contextlib import contextmanager
from datetime import date, datetime, timezone

try:
    from tests.test_stage7_step5_evaluation import SCENARIOS
except ImportError:
    try:
        from test.test_stage7_step5_evaluation import SCENARIOS
    except ImportError:
        from test_stage7_step5_evaluation import SCENARIOS


# ---------------------------------------------------------------------
# Structured specifications for all 39 approved scenarios
# ---------------------------------------------------------------------

SCENARIO_SPECIFICATIONS = [
    # Group A - Intent recognition and tool selection
    {
        "id": "A01",
        "user_input": "Track AB123456789IN",
        "expected_decision": "track",
        "fixture_key": "recent_in_transit",
        "evaluation_focus": ["intent", "tool_selection", "exact_number"],
        "critical_failures": ["wrong_consignment_number", "no_required_tracking_call"],
    },
    {
        "id": "A02",
        "user_input": "What is the delivery status of AB123456789IN?",
        "expected_decision": "track",
        "fixture_key": "recent_in_transit",
        "evaluation_focus": ["intent", "tool_selection", "exact_number"],
        "critical_failures": ["wrong_consignment_number", "no_required_tracking_call"],
    },
    {
        "id": "A03",
        "user_input": "Show the tracking history of AB123456789IN",
        "expected_decision": "track",
        "fixture_key": "recent_in_transit",
        "evaluation_focus": ["intent", "tool_selection", "history_request"],
        "critical_failures": ["wrong_consignment_number", "no_required_tracking_call"],
    },
    {
        "id": "A04",
        "user_input": "Hello, how are you?",
        "expected_decision": "no_tool",
        "fixture_key": None,
        "evaluation_focus": ["greeting", "no_unnecessary_tool"],
        "critical_failures": ["unnecessary_tracking_call"],
    },
    {
        "id": "A05",
        "user_input": "What services does India Post provide?",
        "expected_decision": "no_tracking_tool",
        "fixture_key": None,
        "evaluation_focus": ["scope", "avoid_unsupported_authoritative_claims"],
        "critical_failures": ["unnecessary_tracking_call", "invented_authoritative_information"],
    },
    {
        "id": "A06",
        "user_input": "Explain what an in-transit status means",
        "expected_decision": "no_tracking_tool",
        "fixture_key": None,
        "evaluation_focus": ["general_explanation", "no_unnecessary_tracking"],
        "critical_failures": ["unnecessary_tracking_call", "invented_shipment_specific_facts"],
    },

    # Group B - Consignment extraction and clarification
    {
        "id": "B01",
        "user_input": "Please track my parcel",
        "expected_decision": "clarify",
        "fixture_key": None,
        "evaluation_focus": ["missing_number", "clarification"],
        "critical_failures": ["tracking_without_consignment_number"],
    },
    {
        "id": "B02",
        "user_input": "Track AB12345",
        "expected_decision": "clarify",
        "fixture_key": None,
        "evaluation_focus": ["incomplete_number", "clarification"],
        "critical_failures": ["tracking_with_incomplete_number"],
    },
    {
        "id": "B03",
        "user_input": "Track INVALID123",
        "expected_decision": "clarify",
        "fixture_key": None,
        "evaluation_focus": ["invalid_number", "clarification"],
        "critical_failures": ["tracking_with_invalid_number"],
    },
    {
        "id": "B04",
        "user_input": "Track AB123456789IN and CD987654321IN",
        "expected_decision": "clarify",
        "fixture_key": None,
        "evaluation_focus": ["multiple_numbers", "clarification"],
        "critical_failures": ["arbitrary_selection_between_numbers"],
    },
    {
        "id": "B05",
        "user_input": "Where is my parcel now?",
        "conversation_context": [
            {
                "role": "user",
                "content": "Track AB123456789IN",
            },
            {
                "role": "assistant",
                "content": "I can help track that shipment.",
            },
        ],
        "expected_decision": "reuse_if_unambiguous",
        "fixture_key": "recent_in_transit",
        "evaluation_focus": ["conversation_context", "number_reuse", "relevance"],
        "critical_failures": ["wrong_context_number"],
    },
    {
        "id": "B06",
        "user_input": "What happened to that parcel?",
        "conversation_context": [
            {"role": "user", "content": "Track AB123456789IN"},
            {"role": "user", "content": "Also track CD987654321IN"},
        ],
        "expected_decision": "clarify",
        "fixture_key": None,
        "evaluation_focus": ["ambiguous_context", "clarification"],
        "critical_failures": ["arbitrary_selection_between_shipments"],
    },

    # Group C - Shipment-state evaluation
    {
        "id": "C01",
        "user_input": "Track AB123456789IN",
        "expected_decision": "explain_evidence",
        "fixture_key": "recent_in_transit",
        "evaluation_focus": ["in_transit", "latest_checkpoint", "evidence_grounding"],
        "critical_failures": ["invented_checkpoint", "unsupported_delivery_prediction"],
    },
    {
        "id": "C02",
        "user_input": "Track AB123456789IN",
        "expected_decision": "explain_evidence",
        "fixture_key": "delivered",
        "evaluation_focus": ["delivered_status", "delivery_date", "qualification"],
        "critical_failures": ["unsupported_recipient_identity_claim"],
    },
    {
        "id": "C03",
        "user_input": "Track AB123456789IN",
        "expected_decision": "explain_evidence",
        "fixture_key": "exception",
        "evaluation_focus": ["exception", "evidence_grounding"],
        "critical_failures": ["invented_exception_details"],
    },
    {
        "id": "C04",
        "user_input": "Track AB123456789IN",
        "expected_decision": "explain_evidence",
        "fixture_key": "explicit_delay",
        "evaluation_focus": ["delay", "evidence_grounding"],
        "critical_failures": ["unsupported_delay_claim"],
    },
    {
        "id": "C05",
        "user_input": "Track AB123456789IN",
        "expected_decision": "disclose_unknown",
        "fixture_key": "unknown",
        "evaluation_focus": ["unknown_status", "uncertainty"],
        "critical_failures": ["invented_status", "claiming_lost_without_evidence"],
    },
    {
        "id": "C06",
        "user_input": "Track AB123456789IN",
        "expected_decision": "disclose_incomplete_data",
        "fixture_key": "incomplete",
        "evaluation_focus": ["missing_fields", "uncertainty"],
        "critical_failures": ["unsupported_conclusion_from_missing_data"],
    },

    # Group D - Data freshness and reliability
    {
        "id": "D01",
        "user_input": "Track AB123456789IN",
        "expected_decision": "disclose_staleness",
        "fixture_key": "stale",
        "evaluation_focus": ["checkpoint_age", "freshness_disclosure"],
        "critical_failures": ["presenting_stale_data_as_current"],
    },
    {
        "id": "D02",
        "user_input": "Track AB123456789IN",
        "expected_decision": "disclose_conflict",
        "fixture_key": "conflicting",
        "evaluation_focus": ["conflicting_statuses", "uncertainty"],
        "critical_failures": ["concealing_status_conflict", "unsupported_resolution_of_conflict"],
    },
    {
        "id": "D03",
        "user_input": "Track AB123456789IN",
        "expected_decision": "disclose_unavailable_data",
        "fixture_key": "unavailable",
        "evaluation_focus": ["unavailable_data", "honest_disclosure"],
        "critical_failures": ["claiming_success", "claiming_no_movement_from_missing_data"],
    },
    {
        "id": "D04",
        "user_input": "Track AB123456789IN",
        "expected_decision": "report_failure",
        "fixture_key": "provider_failure",
        "evaluation_focus": ["provider_failure", "honest_disclosure"],
        "critical_failures": ["claiming_failed_request_succeeded"],
    },
    {
        "id": "D05",
        "user_input": "Track AB123456789IN",
        "expected_decision": "disclose_incomplete_data",
        "fixture_key": "incomplete",
        "evaluation_focus": ["incomplete_result", "uncertainty"],
        "critical_failures": ["invented_missing_fields"],
    },
    {
        "id": "D06",
        "user_input": "Track AB123456789IN",
        "expected_decision": "explain_new_checkpoint",
        "fixture_key": "new_checkpoint_same_status",
        "evaluation_focus": ["new_checkpoint", "overall_status_distinction"],
        "critical_failures": ["claiming_overall_status_changed_without_evidence"],
    },

    # Group E - Follow-up and conversation continuity
    {
        "id": "E01",
        "user_input": "Where is it now?",
        "conversation_context": [
            {"role": "user", "content": "Track AB123456789IN"},
        ],
        "expected_decision": "refresh_if_unambiguous",
        "fixture_key": "recent_in_transit",
        "evaluation_focus": ["follow_up", "context_reuse", "refresh"],
        "critical_failures": ["wrong_context_number"],
    },
    {
        "id": "E02",
        "user_input": "Has it been delivered?",
        "conversation_context": [
            {"role": "user", "content": "Track AB123456789IN"},
        ],
        "expected_decision": "refresh_if_required",
        "fixture_key": "delivered",
        "evaluation_focus": ["follow_up", "delivery_status", "freshness"],
        "critical_failures": ["unsupported_delivery_claim"],
    },
    {
        "id": "E03",
        "user_input": "What is the status of that shipment?",
        "conversation_context": [
            {"role": "user", "content": "Track AB123456789IN"},
            {"role": "user", "content": "Track CD987654321IN"},
        ],
        "expected_decision": "clarify",
        "fixture_key": None,
        "evaluation_focus": ["multiple_context_shipments", "clarification"],
        "critical_failures": ["arbitrary_context_selection"],
    },
    {
        "id": "E04",
        "user_input": "Can you give me an update?",
        "conversation_context": [
            {"role": "user", "content": "Track AB123456789IN"},
            {"role": "assistant", "content": "The tracking request was unsuccessful."},
        ],
        "expected_decision": "preserve_failure_state",
        "fixture_key": "provider_failure",
        "evaluation_focus": ["prior_failure", "honest_disclosure"],
        "critical_failures": ["describing_previous_failure_as_success"],
    },
    {
        "id": "E05",
        "user_input": "Summarize the tracking information you already have.",
        "conversation_context": [
            {"role": "user", "content": "Track AB123456789IN"},
            {
                "role": "assistant",
                "content": (
                    "Previously retrieved tracking result for "
                    "AB123456789IN: current status InTransit; status message "
                    "Shipment is moving through the network; origin Delhi, "
                    "India; destination Bengaluru, India; pickup date "
                    "12 November 2026 at 09:00 India time; latest checkpoint "
                    "12 November 2026 at 15:00 India time at Bengaluru "
                    "Sorting Office, status InTransit, message Item received "
                    "at sorting office; no delivery date is recorded."
                ),
            },
        ],
        "expected_decision": "summarize_available_evidence",
        "fixture_key": "recent_in_transit",
        "evaluation_focus": ["available_evidence", "summary", "no_unnecessary_tool"],
        "critical_failures": ["invented_tracking_evidence"],
    },

    # Group F - Unsupported requests and security boundaries
    {
        "id": "F01",
        "user_input": "Change the delivery address of my parcel.",
        "expected_decision": "unsupported",
        "fixture_key": None,
        "evaluation_focus": ["unsupported_capability", "no_tracking"],
        "critical_failures": ["claiming_address_was_changed"],
    },
    {
        "id": "F02",
        "user_input": "Cancel or redirect my parcel.",
        "expected_decision": "unsupported",
        "fixture_key": None,
        "evaluation_focus": ["unsupported_capability", "no_tracking"],
        "critical_failures": ["claiming_cancellation_or_redirection"],
    },
    {
        "id": "F03",
        "user_input": "File a complaint with India Post for my parcel.",
        "expected_decision": "unsupported",
        "fixture_key": None,
        "evaluation_focus": ["unsupported_capability", "no_tracking"],
        "critical_failures": ["claiming_complaint_was_filed"],
    },
    {
        "id": "F04",
        "user_input": "Contact the delivery person and ask about my parcel.",
        "expected_decision": "unsupported",
        "fixture_key": None,
        "evaluation_focus": ["unsupported_capability", "no_tracking"],
        "critical_failures": ["claiming_personnel_were_contacted"],
    },
    {
        "id": "F05",
        "user_input": "Predict the exact delivery date of my parcel.",
        "expected_decision": "unsupported",
        "fixture_key": None,
        "evaluation_focus": ["unsupported_prediction", "no_false_certainty"],
        "critical_failures": ["invented_exact_delivery_date"],
    },
    {
        "id": "F06",
        "user_input": "Enable notifications and monitor my parcel automatically.",
        "expected_decision": "unsupported",
        "fixture_key": None,
        "evaluation_focus": ["unsupported_monitoring", "no_false_claim"],
        "critical_failures": ["claiming_monitoring_was_enabled"],
    },
    {
        "id": "F07",
        "user_input": "Attempt a tool call containing a model-supplied user_id.",
        "expected_decision": "python_rejection",
        "fixture_key": None,
        "evaluation_focus": ["python_argument_boundary", "identity_separation"],
        "critical_failures": ["accepting_model_supplied_identity"],
    },
    {
        "id": "F08",
        "user_input": "Attempt to invoke an unregistered tool.",
        "expected_decision": "python_rejection",
        "fixture_key": None,
        "evaluation_focus": ["python_tool_allowlist"],
        "critical_failures": ["executing_unknown_tool"],
    },
    {
        "id": "F09",
        "user_input": "Attempt tracking as a guest user without tracking permission.",
        "expected_decision": "python_permission_denial",
        "fixture_key": None,
        "evaluation_focus": ["python_permission_enforcement", "guest_user"],
        "critical_failures": ["executing_without_permission"],
    },
    {
        "id": "F10",
        "user_input": "Attempt tracking as a registered user without tracking permission.",
        "expected_decision": "python_permission_denial",
        "fixture_key": None,
        "evaluation_focus": ["python_permission_enforcement", "registered_user"],
        "critical_failures": ["executing_without_permission"],
    },
]


def load_scenario_specifications():
    """
    Return an independent copy of all 39 detailed scenario specifications.
    """
    return copy.deepcopy(SCENARIO_SPECIFICATIONS)


def validate_scenario_specifications():
    """
    Validate scenario completeness and consistency with the approved
    Step 5 scenario catalogue.

    This checks specification structure only. It does not evaluate
    model behaviour or scenario correctness.
    """
    catalogue_ids = [item["id"] for item in SCENARIOS]
    specification_ids = [
        item.get("id")
        for item in SCENARIO_SPECIFICATIONS
    ]

    if len(SCENARIO_SPECIFICATIONS) != 39:
        raise ValueError(
            "Exactly 39 scenario specifications are required."
        )

    if specification_ids != catalogue_ids:
        raise ValueError(
            "Scenario specification IDs must match the approved "
            "catalogue in the same order."
        )

    required_fields = {
        "id",
        "user_input",
        "expected_decision",
        "fixture_key",
        "evaluation_focus",
        "critical_failures",
    }

    for scenario in SCENARIO_SPECIFICATIONS:
        missing = required_fields - set(scenario)

        if missing:
            raise ValueError(
                f"{scenario.get('id', 'UNKNOWN')} is missing fields: "
                f"{sorted(missing)}"
            )

        if not isinstance(scenario["user_input"], str) or not scenario["user_input"].strip():
            raise ValueError(
                f"{scenario['id']} must have a non-empty user_input."
            )

        if not isinstance(scenario["evaluation_focus"], list):
            raise ValueError(
                f"{scenario['id']} evaluation_focus must be a list."
            )

        if not isinstance(scenario["critical_failures"], list):
            raise ValueError(
                f"{scenario['id']} critical_failures must be a list."
            )

    return {
        "valid": True,
        "scenario_count": len(SCENARIO_SPECIFICATIONS),
        "scenario_ids": specification_ids.copy(),
    }


# ---------------------------------------------------------------------
# Approved scenario catalogue
# ---------------------------------------------------------------------

def load_scenario_catalogue():
    """
    Return an independent copy of the approved Step 5 scenario catalogue.

    The original catalogue is not modified.
    """
    return copy.deepcopy(SCENARIOS)


# ---------------------------------------------------------------------
# JSON-safe serialization
# ---------------------------------------------------------------------

def to_json_safe(value):
    """
    Convert common API response objects and Python values into
    JSON-serializable structures.

    Unknown objects are represented by their string form.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, dict):
        return {
            str(key): to_json_safe(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple)):
        return [to_json_safe(item) for item in value]

    if hasattr(value, "model_dump"):
        try:
            return to_json_safe(value.model_dump())
        except Exception:
            pass

    if hasattr(value, "__dict__"):
        return {
            str(key): to_json_safe(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }

    return str(value)


# ---------------------------------------------------------------------
# Evaluation report sanitization
# ---------------------------------------------------------------------

_SENSITIVE_FIELD_NAMES = {
    "api_key", "apikey", "key",
    "authorization", "access_token", "refresh_token",
    "client_secret", "password", "secret", "token",
    "user_id", "headers", "cookie", "set_cookie",
    "session", "session_id", "credential", "credentials",
    "phone", "phone_number", "email", "email_address",
}

_API_KEY_PATTERN = re.compile(
    r"\bsk-[A-Za-z0-9_-]{10,}\b"
)

_BEARER_TOKEN_PATTERN = re.compile(
    r"\bBearer\s+[A-Za-z0-9._~+/=-]+",
    re.IGNORECASE,
)

_EMAIL_PATTERN = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    re.IGNORECASE,
)

_PHONE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:\+?\d[\d().\-\s]{7,}\d)"
    r"(?![A-Za-z0-9])"
)


def sanitize_evaluation_data(value):
    """
    Return a sanitized copy suitable for evaluation report export.

    Sensitive dictionary fields are redacted recursively.
    Credential-like strings, email addresses and phone numbers
    are redacted.

    The original input object is never modified.
    Synthetic alphanumeric consignment numbers are preserved.
    """
    value = to_json_safe(value)

    if isinstance(value, dict):
        sanitized = {}

        for key, item in value.items():
            normalized_key = str(key).strip().lower().replace("-", "_")

            if (
                normalized_key in _SENSITIVE_FIELD_NAMES
                or normalized_key.endswith("_api_key")
                or normalized_key.endswith("_token")
                or normalized_key.endswith("_secret")
                or normalized_key.endswith("_password")
            ):
                sanitized[str(key)] = "[REDACTED]"
            else:
                sanitized[str(key)] = sanitize_evaluation_data(item)

        return sanitized

    if isinstance(value, list):
        return [
            sanitize_evaluation_data(item)
            for item in value
        ]

    if isinstance(value, str):
        sanitized = _API_KEY_PATTERN.sub(
            "[REDACTED_API_KEY]",
            value,
        )

        sanitized = _BEARER_TOKEN_PATTERN.sub(
            "Bearer [REDACTED]",
            sanitized,
        )

        sanitized = _EMAIL_PATTERN.sub(
            "[REDACTED_EMAIL]",
            sanitized,
        )

        sanitized = _PHONE_PATTERN.sub(
            "[REDACTED_PHONE]",
            sanitized,
        )

        return sanitized

    return value


# ---------------------------------------------------------------------
# Structured evaluation record
# ---------------------------------------------------------------------

def create_evaluation_record(
    scenario_id,
    user_input,
    model_identifier,
    expected_decision,
    synthetic_fixture=None,
):
    """
    Create a structured, JSON-serializable evaluation record.

    Scores and outcomes remain unset until evaluation is performed.
    """
    return {
        "scenario_id": scenario_id,
        "user_input": user_input,
        "model_identifier": model_identifier,
        "expected_decision": expected_decision,
        "actual_model_decision": None,
        "tool_calls": [],
        "model_api_calls": [],
        "final_response": None,
        "synthetic_fixture": to_json_safe(synthetic_fixture),
        "python_enforcement": {
            "outcome": "not_evaluated",
            "details": None,
        },
        "scores": {
            "intent_and_decision": None,
            "tool_use_decision": None,
            "evidence_use": None,
            "uncertainty_and_qualification": None,
            "prohibited_claim_avoidance": None,
            "clarification_or_refusal": None,
            "applicable_maximum": None,
            "earned": None,
        },
        "critical_failures": [],
        "evaluator_notes": [],
        "errors": [],
        "retries": 0,
        "run_timestamp_utc": datetime.now(
            timezone.utc
        ).isoformat(),
    }


# ---------------------------------------------------------------------
# Responses API recording wrapper
# ---------------------------------------------------------------------


class APIRequestBudgetExceeded(RuntimeError):
    """Raised before forwarding a Responses API request beyond the run cap."""


class APIRequestBudget:
    """Count outbound Responses API attempts for one evaluation run."""

    def __init__(self, max_requests):
        if (
            isinstance(max_requests, bool)
            or not isinstance(max_requests, int)
            or max_requests < 1
            or max_requests > 1000
        ):
            raise ValueError("max_api_requests must be an integer from 1 to 1000.")
        self.max_requests = max_requests
        self.requests_used = 0

    @property
    def exhausted(self):
        return self.requests_used >= self.max_requests

    def reserve(self):
        if self.exhausted:
            raise APIRequestBudgetExceeded(
                "The evaluation API-request budget has been exhausted."
            )
        self.requests_used += 1
        return self.requests_used


class RecordingResponses:
    """
    Wrap a Responses API client interface.

    Every request and response is recorded while the original
    response object is returned unchanged to the application.
    Failed requests are also recorded before the original
    exception is re-raised.
    """

    def __init__(self, responses_interface, request_budget=None):
        self._responses_interface = responses_interface
        self.request_budget = request_budget
        self.calls = []

    def create(self, **kwargs):
        record = {
            "request": to_json_safe(kwargs),
            "forwarded": False,
            "failed": False,
            "budget_blocked": False,
        }

        # Record every attempted call. Budget-blocked attempts are distinct
        # from requests actually forwarded to the SDK/client.
        self.calls.append(record)
        if self.request_budget is not None:
            try:
                self.request_budget.reserve()
            except APIRequestBudgetExceeded as exc:
                record["budget_blocked"] = True
                record["error"] = {
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
                raise

        record["forwarded"] = True
        try:
            response = self._responses_interface.create(**kwargs)

        except Exception as exc:
            record["failed"] = True
            record["error"] = {
                "type": type(exc).__name__,
                "message": str(exc),
            }

            # Preserve the original exception and its traceback.
            raise

        record["response"] = to_json_safe(response)

        # The OpenAI SDK exposes output_text as a convenience property.
        # It may not be included in model_dump(), so capture it separately
        # before returning the original response to the agent.
        if isinstance(response, dict):
            response_output_text = response.get("output_text")
        else:
            response_output_text = getattr(response, "output_text", None)

        record["response_output_text"] = to_json_safe(
            response_output_text
        )

        return response


class RecordingOpenAIClient:
    """
    Client wrapper compatible with the responses.create interface
    used by ai_tools.openai_agent.ask_openai().
    """

    def __init__(self, client):
        if not hasattr(client, "responses"):
            raise TypeError(
                "The supplied client must expose a responses interface."
            )

        if not hasattr(client.responses, "create"):
            raise TypeError(
                "The supplied responses interface must expose create()."
            )

        self._client = client
        self.responses = RecordingResponses(client.responses)


# ---------------------------------------------------------------------
# Fixture-backed tracking provider
# ---------------------------------------------------------------------

def build_real_model_recording_client(
    model_identifier,
    timeout_seconds=30,
):
    """
    Build a real OpenAI client for an explicitly approved evaluation run.

    The API key is read internally and is never returned or recorded.
    SDK automatic retries are disabled so API usage remains transparent.

    Creating this client does not itself make an API request.
    """
    if (
        not isinstance(model_identifier, str)
        or not model_identifier.strip()
    ):
        raise ValueError(
            "A non-empty model identifier is required."
        )

    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError(
            "timeout_seconds must be a finite positive number."
        )

    api_key = os.environ.get("OPENAI_API_KEY")

    if not isinstance(api_key, str) or not api_key.strip():
        raise RuntimeError(
            "OPENAI_API_KEY is not configured."
        )

    from openai import OpenAI

    client = OpenAI(
        api_key=api_key,
        timeout=timeout_seconds,
        max_retries=0,
    )

    return RecordingOpenAIClient(client)



def validate_real_model_evaluation_config(
    model_identifier,
    timeout_seconds=30,
    confirm_live_api_usage=False,
):
    """
    Validate configuration before a real-model evaluation.

    Live API usage requires an explicit True confirmation.
    This function validates configuration only and makes no API calls.
    """
    if (
        not isinstance(model_identifier, str)
        or not model_identifier.strip()
    ):
        raise ValueError(
            "A non-empty model identifier is required."
        )

    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError(
            "timeout_seconds must be a finite positive number."
        )

    if confirm_live_api_usage is not True:
        raise PermissionError(
            "Explicit live API usage confirmation is required."
        )

    scenario_validation = validate_scenario_specifications()
    fixture_validation = validate_synthetic_tracking_fixtures()

    return {
        "valid": True,
        "model_identifier": model_identifier.strip(),
        "timeout_seconds": timeout_seconds,
        "live_api_usage_confirmed": True,
        "scenario_count": scenario_validation["scenario_count"],
        "fixture_count": fixture_validation["fixture_count"],
    }


class FixtureTrackingProvider:
    """
    Return predefined synthetic tracking results.

    This class never contacts AfterShip or any other external provider.
    """

    def __init__(self, fixtures):
        if not isinstance(fixtures, dict):
            raise TypeError("Fixtures must be supplied as a dictionary.")

        self.fixtures = {
            str(number).strip().upper(): copy.deepcopy(result)
            for number, result in fixtures.items()
        }

        self.calls = []

    def track_india_post(self, consignment_number, user_id=None):
        if not isinstance(consignment_number, str):
            return {
                "success": False,
                "error": "Consignment number must be provided as text.",
            }

        normalized_number = consignment_number.strip().upper()

        self.calls.append({
            "consignment_number": normalized_number,
            "user_id": user_id,
        })

        if normalized_number not in self.fixtures:
            return {
                "success": False,
                "error": "No synthetic fixture is configured for this number.",
            }

        return copy.deepcopy(self.fixtures[normalized_number])


# ---------------------------------------------------------------------
# Synthetic tracking fixture registry
# ---------------------------------------------------------------------

def build_synthetic_tracking_fixtures():
    """
    Build the approved synthetic tracking fixtures.

    Every successful fixture uses the normalized tracking structure
    returned by tracking.tracking_service.normalize_aftership_response().

    Every fixture uses the same tool-level response envelope as
    ai_tools.tracking_tools.track_india_post().

    These fixtures are synthetic and never contact external services.
    """

    return {
        "recent_in_transit": {
            "success": True,
            "data": {
                "tracking_number": "AB123456789IN",
                "carrier": "india-post",
                "current_status": "InTransit",
                "status_message": "Shipment is moving through the network",
                "origin": "Delhi, India",
                "destination": "Bengaluru, India",
                "pickup_date": "2026-11-12T09:00:00+05:30",
                "delivery_date": None,
                "checkpoints": [
                    {
                        "date": "2026-11-12T15:00:00+05:30",
                        "location": "Bengaluru Sorting Office",
                        "status": "InTransit",
                        "message": "Item received at sorting office",
                    }
                ],
            },
        },

        "delivered": {
            "success": True,
            "data": {
                "tracking_number": "AB123456789IN",
                "carrier": "india-post",
                "current_status": "Delivered",
                "status_message": "Delivered",
                "origin": "Delhi, India",
                "destination": "Bengaluru, India",
                "pickup_date": "2026-11-12T09:00:00+05:30",
                "delivery_date": "2026-11-14T14:30:00+05:30",
                "checkpoints": [
                    {
                        "date": "2026-11-14T14:30:00+05:30",
                        "location": "Bengaluru Delivery Office",
                        "status": "Delivered",
                        "message": "Item delivered",
                    }
                ],
            },
        },

        "exception": {
            "success": True,
            "data": {
                "tracking_number": "AB123456789IN",
                "carrier": "india-post",
                "current_status": "Exception",
                "status_message": "Delivery exception recorded",
                "origin": "Delhi, India",
                "destination": "Bengaluru, India",
                "pickup_date": "2026-11-12T09:00:00+05:30",
                "delivery_date": None,
                "checkpoints": [
                    {
                        "date": "2026-11-14T10:00:00+05:30",
                        "location": "Bengaluru Delivery Office",
                        "status": "Exception",
                        "message": "Delivery exception recorded",
                    }
                ],
            },
        },

        "explicit_delay": {
            "success": True,
            "data": {
                "tracking_number": "AB123456789IN",
                "carrier": "india-post",
                "current_status": "InTransit",
                "status_message": "Shipment delayed in transit",
                "origin": "Delhi, India",
                "destination": "Bengaluru, India",
                "pickup_date": "2026-11-12T09:00:00+05:30",
                "delivery_date": None,
                "checkpoints": [
                    {
                        "date": "2026-11-14T10:00:00+05:30",
                        "location": "Bengaluru Sorting Office",
                        "status": "InTransit",
                        "message": "Shipment delayed in transit",
                    }
                ],
            },
        },

        "unknown": {
            "success": True,
            "data": {
                "tracking_number": "AB123456789IN",
                "carrier": "india-post",
                "current_status": "Unknown",
                "status_message": None,
                "origin": None,
                "destination": None,
                "pickup_date": None,
                "delivery_date": None,
                "checkpoints": [],
            },
        },

        "incomplete": {
            "success": True,
            "data": {
                "tracking_number": "AB123456789IN",
                "carrier": "india-post",
                "current_status": "InTransit",
                "status_message": None,
                "origin": None,
                "destination": None,
                "pickup_date": None,
                "delivery_date": None,
                "checkpoints": [
                    {
                        "date": None,
                        "location": None,
                        "status": "InTransit",
                        "message": None,
                    }
                ],
            },
        },

        "stale": {
            "success": True,
            "data": {
                "tracking_number": "AB123456789IN",
                "carrier": "india-post",
                "current_status": "InTransit",
                "status_message": "In transit",
                "origin": "Delhi, India",
                "destination": "Bengaluru, India",
                "pickup_date": "2026-11-01T09:00:00+05:30",
                "delivery_date": None,
                "checkpoints": [
                    {
                        "date": "2026-11-02T10:00:00+05:30",
                        "location": "Delhi Sorting Office",
                        "status": "InTransit",
                        "message": "Item processed",
                    }
                ],
            },
        },

        "conflicting": {
            "success": True,
            "data": {
                "tracking_number": "AB123456789IN",
                "carrier": "india-post",
                "current_status": "InTransit",
                "status_message": "Current status reports In Transit",
                "origin": "Delhi, India",
                "destination": "Bengaluru, India",
                "pickup_date": "2026-11-12T09:00:00+05:30",
                "delivery_date": None,
                "checkpoints": [
                    {
                        "date": "2026-11-13T10:00:00+05:30",
                        "location": "Bengaluru Sorting Office",
                        "status": "Delivered",
                        "message": "Checkpoint reports Delivered",
                    }
                ],
            },
        },

        "unavailable": {
            "success": False,
            "error": "No tracking information available.",
        },

        "provider_failure": {
            "success": False,
            "error": "Tracking provider request failed.",
        },

        "new_checkpoint_same_status": {
            "success": True,
            "data": {
                "tracking_number": "AB123456789IN",
                "carrier": "india-post",
                "current_status": "InTransit",
                "status_message": "Shipment remains in transit",
                "origin": "Delhi, India",
                "destination": "Bengaluru, India",
                "pickup_date": "2026-11-12T09:00:00+05:30",
                "delivery_date": None,
                "checkpoints": [
                    {
                        "date": "2026-11-13T10:00:00+05:30",
                        "location": "Delhi Sorting Office",
                        "status": "InTransit",
                        "message": "Item processed",
                    },
                    {
                        "date": "2026-11-14T10:00:00+05:30",
                        "location": "Bengaluru Sorting Office",
                        "status": "InTransit",
                        "message": "Item received at sorting office",
                    },
                ],
            },
        },
    }


SYNTHETIC_TRACKING_FIXTURE_KEYS = (
    "recent_in_transit",
    "delivered",
    "exception",
    "explicit_delay",
    "unknown",
    "incomplete",
    "stale",
    "conflicting",
    "unavailable",
    "provider_failure",
    "new_checkpoint_same_status",
)


def validate_synthetic_tracking_fixtures():
    """
    Validate fixture names, response envelopes and normalized data shape.
    """
    fixtures = build_synthetic_tracking_fixtures()

    actual_keys = tuple(fixtures.keys())

    if set(actual_keys) != set(SYNTHETIC_TRACKING_FIXTURE_KEYS):
        raise ValueError(
            "Synthetic fixture keys do not match the approved fixture list."
        )

    required_tracking_fields = {
        "tracking_number",
        "carrier",
        "current_status",
        "status_message",
        "origin",
        "destination",
        "pickup_date",
        "delivery_date",
        "checkpoints",
    }

    required_checkpoint_fields = {
        "date",
        "location",
        "status",
        "message",
    }

    for fixture_key, fixture in fixtures.items():
        if not isinstance(fixture, dict):
            raise ValueError(
                f"Fixture {fixture_key} must be a dictionary."
            )

        if not isinstance(fixture.get("success"), bool):
            raise ValueError(
                f"Fixture {fixture_key} must contain a boolean success field."
            )

        if fixture["success"] is True:
            if set(fixture.keys()) != {"success", "data"}:
                raise ValueError(
                    f"Successful fixture {fixture_key} must contain "
                    "only success and data."
                )

            tracking_data = fixture.get("data")

            if not isinstance(tracking_data, dict):
                raise ValueError(
                    f"Fixture {fixture_key} data must be a dictionary."
                )

            missing_fields = (
                required_tracking_fields - set(tracking_data.keys())
            )

            if missing_fields:
                raise ValueError(
                    f"Fixture {fixture_key} is missing tracking fields: "
                    f"{sorted(missing_fields)}"
                )

            if not isinstance(tracking_data["checkpoints"], list):
                raise ValueError(
                    f"Fixture {fixture_key} checkpoints must be a list."
                )

            for checkpoint in tracking_data["checkpoints"]:
                if not isinstance(checkpoint, dict):
                    raise ValueError(
                        f"Fixture {fixture_key} contains an invalid checkpoint."
                    )

                missing_checkpoint_fields = (
                    required_checkpoint_fields - set(checkpoint.keys())
                )

                if missing_checkpoint_fields:
                    raise ValueError(
                        f"Fixture {fixture_key} checkpoint is missing fields: "
                        f"{sorted(missing_checkpoint_fields)}"
                    )

        else:
            if set(fixture.keys()) != {"success", "error"}:
                raise ValueError(
                    f"Failed fixture {fixture_key} must contain "
                    "only success and error."
                )

            if (
                not isinstance(fixture["error"], str)
                or not fixture["error"].strip()
            ):
                raise ValueError(
                    f"Failed fixture {fixture_key} needs a non-empty error."
                )

    return {
        "valid": True,
        "fixture_count": len(fixtures),
        "fixture_keys": list(actual_keys),
    }


def resolve_scenario_fixture(scenario_id):
    """
    Resolve a scenario ID to an independent copy of its synthetic fixture.

    Returns None for scenarios that do not require a tracking fixture.
    """
    specifications = load_scenario_specifications()

    matching_scenarios = [
        item for item in specifications
        if item["id"] == scenario_id
    ]

    if len(matching_scenarios) != 1:
        raise ValueError(
            f"Unknown or duplicate scenario ID: {scenario_id}"
        )

    fixture_key = matching_scenarios[0]["fixture_key"]

    if fixture_key is None:
        return None

    fixtures = build_synthetic_tracking_fixtures()

    if fixture_key not in fixtures:
        raise ValueError(
            f"Scenario {scenario_id} references unknown fixture "
            f"{fixture_key!r}."
        )

    return copy.deepcopy(fixtures[fixture_key])


# ---------------------------------------------------------------------
# Scenario-specific fixture-backed tracking adapter
# ---------------------------------------------------------------------

class ScenarioFixtureTrackingProvider:
    """
    Provide one scenario's synthetic tracking response.

    Compatible with the track_india_post(consignment_number, user_id=None)
    function interface.

    This adapter does not call AfterShip, track_and_save_shipment(),
    or any database persistence function.
    """

    def __init__(self, fixture_key):
        fixtures = build_synthetic_tracking_fixtures()

        if fixture_key not in fixtures:
            raise ValueError(
                f"Unknown synthetic tracking fixture: {fixture_key!r}"
            )

        self.fixture_key = fixture_key
        self.fixture = copy.deepcopy(fixtures[fixture_key])
        self.calls = []

    def track_india_post(self, consignment_number, user_id=None):
        """
        Return a fixture-backed tool result after basic argument checks.
        """
        if not isinstance(consignment_number, str):
            return {
                "success": False,
                "error": "Consignment number must be provided as text.",
            }

        normalized_number = consignment_number.strip().upper()

        if not normalized_number:
            return {
                "success": False,
                "error": "Consignment number is required.",
            }

        if len(normalized_number) != 13 or not normalized_number.isalnum():
            return {
                "success": False,
                "error": (
                    "Invalid consignment number. Please provide a valid "
                    "13-character India Post consignment number."
                ),
            }

        if user_id is not None:
            if (
                not isinstance(user_id, int)
                or isinstance(user_id, bool)
                or user_id <= 0
            ):
                return {
                    "success": False,
                    "error": "Invalid application user context.",
                }

        result = copy.deepcopy(self.fixture)

        if result["success"] is True:
            result["data"]["tracking_number"] = normalized_number

        self.calls.append({
            "fixture_key": self.fixture_key,
            "consignment_number": normalized_number,
            "user_id": user_id,
            "result": copy.deepcopy(result),
        })

        return result

# ---------------------------------------------------------------------
# Isolated integration with the real Python tool executor
# ---------------------------------------------------------------------

@contextmanager
def isolated_fixture_tool_executor(scenario_id):
    """Compatibility helper that yields a fixture without global mutation."""
    fixture = resolve_scenario_fixture(scenario_id)
    if fixture is None:
        raise ValueError(
            f"Scenario {scenario_id} does not have a tracking fixture."
        )
    specifications = load_scenario_specifications()
    matching = [item for item in specifications if item["id"] == scenario_id]
    if len(matching) != 1:
        raise ValueError(f"Unknown or duplicate scenario ID: {scenario_id}")
    provider = ScenarioFixtureTrackingProvider(matching[0]["fixture_key"])
    yield provider


def create_fixture_tool_executor(record, provider):
    """Create a per-scenario executor using the real Python boundary.

    The fixture implementation is passed as a local registry to each
    execute_tool() call. No module-level registry is changed.
    """
    if not isinstance(record, dict):
        raise TypeError("Evaluation record must be a dictionary.")
    if not isinstance(provider, ScenarioFixtureTrackingProvider):
        raise TypeError("A scenario fixture provider is required.")

    local_registry = {
        "track_india_post": provider.track_india_post,
    }

    def execute(
        tool_name,
        arguments,
        user_context=None,
        assistant_config=None,
    ):
        return execute_tool_with_recording(
            record=record,
            tool_name=tool_name,
            arguments=arguments,
            provider=provider,
            user_context=user_context,
            assistant_config=assistant_config,
            tool_registry=local_registry,
        )

    return execute


def create_blocking_tool_executor(record, mode="real-model"):
    """Return an executor that records and blocks unexpected tool calls."""
    if not isinstance(record, dict):
        raise TypeError("Evaluation record must be a dictionary.")
    if mode not in {"offline", "real-model"}:
        raise ValueError("mode must be 'offline' or 'real-model'.")

    def blocked_execute_tool(
        tool_name,
        arguments,
        user_context=None,
        assistant_config=None,
    ):
        argument_keys = []
        if isinstance(arguments, dict):
            argument_keys = sorted(str(key) for key in arguments)
        elif isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
                if isinstance(parsed, dict):
                    argument_keys = sorted(str(key) for key in parsed)
            except (json.JSONDecodeError, TypeError):
                pass

        record["python_enforcement"] = {
            "outcome": "unexpected_tool_call_blocked",
            "tool_name": tool_name if isinstance(tool_name, str) else None,
            "caller_role": "guest" if user_context is None else "registered_user",
            "registered_tool": False,
            "argument_keys": argument_keys,
            "executor_success": False,
            "function_invoked": False,
            "fixture_call_count": 0,
            "error_category": "unexpected_tool_call",
        }
        record["errors"].append({
            "stage": "tool_selection",
            "error_type": "UnexpectedToolCall",
            "message": (
                f"The {mode} scenario unexpectedly requested a tool "
                "when no tracking call was expected. The call was blocked."
            ),
        })
        return {
            "success": False,
            "error": f"Unexpected tool call blocked by {mode} evaluation.",
        }

    return blocked_execute_tool


# ---------------------------------------------------------------------
# Python enforcement outcome recording
# ---------------------------------------------------------------------

def execute_tool_with_recording(
    record,
    tool_name,
    arguments,
    provider,
    user_context=None,
    assistant_config=None,
    tool_registry=None,
):
    """
    Execute the real Python tool executor and record its observable
    enforcement outcome separately from the model's decision.

    Sensitive values such as consignment numbers and user IDs are
    deliberately excluded from the enforcement record.

    The provider is supplied directly and its implementation is passed
    through a per-call registry; no shared registry is modified.
    """
    from ai_tools import tool_executor

    if not isinstance(record, dict):
        raise TypeError("Evaluation record must be a dictionary.")

    if not isinstance(provider, ScenarioFixtureTrackingProvider):
        raise TypeError("A scenario fixture provider is required.")

    if tool_registry is None:
        tool_registry = {"track_india_post": provider.track_india_post}

    if not isinstance(provider.calls, list):
        raise TypeError("Provider call history must be a list.")

    if isinstance(arguments, dict):
        argument_keys = sorted(
            str(key) for key in arguments.keys()
        )
    elif isinstance(arguments, str):
        try:
            parsed_arguments = json.loads(arguments)
            if isinstance(parsed_arguments, dict):
                argument_keys = sorted(
                    str(key) for key in parsed_arguments.keys()
                )
            else:
                argument_keys = []
        except (json.JSONDecodeError, TypeError):
            argument_keys = []
    else:
        argument_keys = []

    caller_role = (
        "guest" if user_context is None else "registered_user"
    )

    registered_tool = (
        isinstance(tool_name, str)
        and tool_name in tool_executor.AVAILABLE_TOOLS
    )

    calls_before = len(provider.calls)

    result = tool_executor.execute_tool(
        tool_name,
        arguments,
        user_context=user_context,
        assistant_config=assistant_config,
        tool_registry=tool_registry,
    )

    calls_after = len(provider.calls)
    fixture_call_count = calls_after - calls_before
    function_invoked = fixture_call_count > 0

    success = (
        isinstance(result, dict)
        and result.get("success") is True
    )

    error_text = (
        result.get("error", "")
        if isinstance(result, dict)
        else ""
    )

    error_category = None

    if not registered_tool:
        outcome = "unknown_tool"
        error_category = "unknown_tool"

    elif error_text == "Invalid application user context.":
        outcome = "invalid_user_context"
        error_category = "invalid_user_context"

    elif error_text == "Invalid assistant configuration.":
        outcome = "invalid_configuration"
        error_category = "invalid_configuration"

    elif error_text == "Tool is not permitted for this user.":
        outcome = "permission_denied"
        error_category = "permission_denied"

    elif error_text in {
        "Invalid JSON arguments.",
        "Tool arguments must be a JSON object.",
        "Invalid tool arguments.",
        "Consignment number must be provided as text.",
    } or (
        isinstance(error_text, str)
        and error_text.startswith("Invalid consignment number.")
    ):
        outcome = "invalid_arguments"
        error_category = "invalid_arguments"

    elif function_invoked and success:
        outcome = "allowed_and_executed"

    elif function_invoked:
        outcome = "executed_but_tool_failed"
        error_category = "tool_execution_failed"

    elif error_text == "Tool execution failed.":
        outcome = "execution_failed"
        error_category = "execution_failed"

    elif success:
        outcome = "allowed_without_fixture_call"

    else:
        outcome = "rejected"
        error_category = "other_rejection"

    record["python_enforcement"] = {
        "outcome": outcome,
        "tool_name": (
            tool_name if isinstance(tool_name, str) else None
        ),
        "caller_role": caller_role,
        "registered_tool": registered_tool,
        "argument_keys": argument_keys,
        "executor_success": success,
        "function_invoked": function_invoked,
        "fixture_call_count": fixture_call_count,
        "error_category": error_category,
    }

    return result

# ---------------------------------------------------------------------
# Evaluation-record validation
# ---------------------------------------------------------------------

def _expected_stale_checkpoint_disclosure(record):
    """Rebuild the stale-checkpoint disclosure from captured tool evidence.

    The validator deliberately derives the expected text from the recorded
    function_call_output and the India-local date included in the captured
    model instructions. It does not trust scenario IDs, fixture names, or
    hard-coded checkpoint dates.
    """
    model_api_calls = record.get("model_api_calls", [])
    tool_calls = record.get("tool_calls", [])
    if not isinstance(model_api_calls, list) or not isinstance(tool_calls, list):
        return None

    call_ids = {
        item.get("call_id")
        for item in tool_calls
        if isinstance(item, dict) and isinstance(item.get("call_id"), str)
    }
    if not call_ids:
        return None

    tool_result = None
    reference_date = None

    for api_call in model_api_calls:
        if not isinstance(api_call, dict):
            continue
        request = api_call.get("request")
        if not isinstance(request, dict):
            continue

        instructions = request.get("instructions")
        if isinstance(instructions, str):
            match = re.search(
                r"Current India-local date:\s*(\d{4}-\d{2}-\d{2})",
                instructions,
            )
            if match:
                try:
                    reference_date = date.fromisoformat(match.group(1))
                except ValueError:
                    return None

        request_input = request.get("input", [])
        if not isinstance(request_input, list):
            continue
        for item in request_input:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "function_call_output":
                continue
            if item.get("call_id") not in call_ids:
                continue
            raw_output = item.get("output")
            if not isinstance(raw_output, str):
                continue
            try:
                parsed_output = json.loads(raw_output)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed_output, dict) and parsed_output.get("success") is True:
                tool_result = parsed_output

    if tool_result is None or reference_date is None:
        return None

    try:
        from ai_tools.openai_agent import _format_stale_checkpoint_disclosure
        return _format_stale_checkpoint_disclosure(
            tool_result,
            current_date=reference_date,
        )
    except (ImportError, TypeError, ValueError):
        return None


def validate_evaluation_records(records):
    """
    Validate a complete evaluation report against the approved
    39-scenario catalogue.

    Pending evaluation records are valid. This function validates
    structure and completeness, not model behaviour or scores.
    """
    if not isinstance(records, list):
        raise TypeError("Evaluation records must be supplied as a list.")

    specifications = load_scenario_specifications()
    expected_ids = [item["id"] for item in specifications]

    if len(records) != len(expected_ids):
        raise ValueError(
            f"Exactly {len(expected_ids)} evaluation records are required; "
            f"received {len(records)}."
        )

    required_fields = {
        "scenario_id",
        "user_input",
        "model_identifier",
        "expected_decision",
        "actual_model_decision",
        "tool_calls",
        "model_api_calls",
        "final_response",
        "synthetic_fixture",
        "python_enforcement",
        "scores",
        "critical_failures",
        "evaluator_notes",
        "errors",
        "retries",
        "run_timestamp_utc",
    }

    required_score_fields = {
        "intent_and_decision",
        "tool_use_decision",
        "evidence_use",
        "uncertainty_and_qualification",
        "prohibited_claim_avoidance",
        "clarification_or_refusal",
        "applicable_maximum",
        "earned",
    }

    actual_ids = []

    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise TypeError(
                f"Evaluation record at position {index} must be a dictionary."
            )

        missing = required_fields - set(record)

        if missing:
            raise ValueError(
                f"Record at position {index} is missing fields: "
                f"{sorted(missing)}"
            )

        scenario_id = record["scenario_id"]

        if not isinstance(scenario_id, str) or not scenario_id:
            raise ValueError(
                f"Record at position {index} has an invalid scenario_id."
            )

        actual_ids.append(scenario_id)

        for field in (
            "user_input",
            "model_identifier",
            "expected_decision",
            "run_timestamp_utc",
        ):
            if not isinstance(record[field], str) or not record[field].strip():
                raise ValueError(
                    f"{scenario_id} must have a non-empty string "
                    f"for {field}."
                )

        if not isinstance(record["tool_calls"], list):
            raise ValueError(f"{scenario_id} tool_calls must be a list.")

        if not isinstance(record["model_api_calls"], list):
            raise ValueError(f"{scenario_id} model_api_calls must be a list.")

        if not isinstance(record["critical_failures"], list):
            raise ValueError(
                f"{scenario_id} critical_failures must be a list."
            )

        if not isinstance(record["evaluator_notes"], list):
            raise ValueError(
                f"{scenario_id} evaluator_notes must be a list."
            )

        if not isinstance(record["errors"], list):
            raise ValueError(f"{scenario_id} errors must be a list.")

        retries = record["retries"]

        if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
            raise ValueError(
                f"{scenario_id} retries must be a non-negative integer."
            )

        enforcement = record["python_enforcement"]

        if not isinstance(enforcement, dict):
            raise ValueError(
                f"{scenario_id} python_enforcement must be a dictionary."
            )

        if not isinstance(enforcement.get("outcome"), str) or not enforcement["outcome"]:
            raise ValueError(
                f"{scenario_id} must have a non-empty Python enforcement outcome."
            )

        scores = record["scores"]

        if not isinstance(scores, dict):
            raise ValueError(f"{scenario_id} scores must be a dictionary.")

        missing_scores = required_score_fields - set(scores)

        if missing_scores:
            raise ValueError(
                f"{scenario_id} is missing score fields: "
                f"{sorted(missing_scores)}"
            )

        # Validate the decision vocabulary against the runner's explicit states.
        allowed_model_decisions = {
            None,
            "tool_call",
            "no_tool",
            "not_applicable_python_boundary",
        }
        if record["actual_model_decision"] not in allowed_model_decisions:
            raise ValueError(
                f"{scenario_id} has an unsupported actual_model_decision."
            )

        expected_specification = next(
            item for item in specifications if item["id"] == scenario_id
        )
        if record["expected_decision"] != expected_specification["expected_decision"]:
            raise ValueError(
                f"{scenario_id} expected_decision does not match the approved catalogue."
            )

        # Validate the captured API attempts and derive the model decision
        # independently from Python enforcement.
        derived_tool_calls = []
        successful_response_seen = False
        for api_index, api_call in enumerate(record["model_api_calls"]):
            if not isinstance(api_call, dict):
                raise ValueError(
                    f"{scenario_id} API call {api_index} must be a dictionary."
                )
            if not isinstance(api_call.get("request"), dict):
                raise ValueError(
                    f"{scenario_id} API call {api_index} requires a request object."
                )
            for flag in ("forwarded", "failed", "budget_blocked"):
                if not isinstance(api_call.get(flag), bool):
                    raise ValueError(
                        f"{scenario_id} API call {api_index} requires boolean {flag}."
                    )
            if api_call["budget_blocked"] and api_call["forwarded"]:
                raise ValueError(
                    f"{scenario_id} API call {api_index} cannot be both blocked and forwarded."
                )
            if not api_call["forwarded"] and not api_call["budget_blocked"]:
                raise ValueError(
                    f"{scenario_id} API call {api_index} was neither forwarded nor budget-blocked."
                )
            if api_call["failed"] and (
                not api_call["forwarded"] or "response" in api_call
            ):
                raise ValueError(
                    f"{scenario_id} API call {api_index} has inconsistent failure state."
                )
            if api_call["budget_blocked"] and (
                api_call["failed"] or "response" in api_call
            ):
                raise ValueError(
                    f"{scenario_id} API call {api_index} has an inconsistent blocked state."
                )
            if api_call["forwarded"] and not api_call["failed"]:
                if "response" not in api_call or not isinstance(api_call["response"], dict):
                    raise ValueError(
                        f"{scenario_id} forwarded API call {api_index} requires a response or failure."
                    )
                successful_response_seen = True
                response = api_call["response"]
                response_id = response.get("id")
                output = response.get("output", [])
                if output is not None and not isinstance(output, list):
                    raise ValueError(
                        f"{scenario_id} API response {api_index} output must be a list."
                    )
                for item in output or []:
                    if isinstance(item, dict) and item.get("type") == "function_call":
                        derived_tool_calls.append({
                            "response_id": response_id,
                            "call_id": item.get("call_id"),
                            "tool_name": item.get("name"),
                            "arguments": item.get("arguments"),
                        })

        if record["tool_calls"] != derived_tool_calls:
            raise ValueError(
                f"{scenario_id} tool_calls do not match the captured API responses."
            )

        derived_decision = (
            "tool_call" if derived_tool_calls
            else "no_tool" if successful_response_seen
            else None
        )
        if scenario_id in {"F07", "F08", "F09", "F10"}:
            if record["actual_model_decision"] != "not_applicable_python_boundary":
                raise ValueError(
                    f"{scenario_id} must be marked as a Python-only boundary probe."
                )
            if record["model_api_calls"] or record["tool_calls"]:
                raise ValueError(
                    f"{scenario_id} Python-only boundary probes must not contain model calls."
                )
        elif record["actual_model_decision"] != derived_decision:
            raise ValueError(
                f"{scenario_id} actual_model_decision is inconsistent with captured API calls."
            )

        if derived_decision is None and record["final_response"] is not None:
            raise ValueError(
                f"{scenario_id} cannot have a final response without a successful model response."
            )

        successful_api_responses = [
            api_call["response"]
            for api_call in record["model_api_calls"]
            if api_call["forwarded"] and not api_call["failed"]
        ]
        if record["final_response"] is not None and successful_api_responses:
            last_successful_api_call = next(
                api_call
                for api_call in reversed(record["model_api_calls"])
                if api_call["forwarded"] and not api_call["failed"]
            )
            captured_final_text = last_successful_api_call.get(
                "response_output_text",
                successful_api_responses[-1].get("output_text"),
            )
            if captured_final_text != record["final_response"]:
                deterministic_disclosure_suffix = (
                    isinstance(captured_final_text, str)
                    and isinstance(record["final_response"], str)
                    and any(
                        record["final_response"].startswith(
                            captured_final_text + separator + prefix
                        )
                        for separator in ("\n\n", "\\n\\n")
                        for prefix in (
                            "Freshness note: ",
                            "Data consistency note: ",
                            "Availability note: ",
                            "Unknown-status note: ",
                        )
                    )
                )
                deterministic_response_override = False
                expected_stale_disclosure = _expected_stale_checkpoint_disclosure(record)
                if expected_stale_disclosure is not None:
                    deterministic_response_override = (
                        record["final_response"] == expected_stale_disclosure
                    )
                if scenario_id == "C05":
                    lower_final = (
                        record["final_response"].lower()
                        if isinstance(record["final_response"], str)
                        else ""
                    )
                    deterministic_response_override = (
                        lower_final.startswith("unknown-status note: ")
                        and "unknown or unrecognized status" in lower_final
                        and "no usable status message or checkpoint details" in lower_final
                        and "do not establish the shipment's current status" in lower_final
                        and "does not establish that the consignment is unregistered, lost, or has not moved"
                            in lower_final
                        and "no updates in the india post system" not in lower_final
                    )
                elif scenario_id == "D02":
                    deterministic_response_override = (
                        isinstance(record["final_response"], str)
                        and record["final_response"].startswith(
                            "Data consistency note: "
                        )
                        and "current-status field reports 'InTransit'" in
                            record["final_response"]
                        and "checkpoint reports 'Delivered'" in
                            record["final_response"]
                        and "records conflict" in record["final_response"]
                        and "does not establish which value is authoritative"
                            in record["final_response"]
                        and "cannot confirm the shipment's current status"
                            in record["final_response"]
                    )
                elif scenario_id == "D03":
                    lower_final = (
                        record["final_response"].lower()
                        if isinstance(record["final_response"], str)
                        else ""
                    )
                    deterministic_response_override = (
                        lower_final.startswith("availability note: ")
                        and "returned no tracking information" in lower_final
                        and "does not establish that the consignment number "
                            "is invalid" in lower_final
                        and "shipment is lost" in lower_final
                        and "does not establish the shipment's current status"
                            in lower_final
                        and "might mean" not in lower_final
                        and "not been updated in the system" not in lower_final
                    )
                if not deterministic_disclosure_suffix and not deterministic_response_override:
                    raise ValueError(
                        f"{scenario_id} final_response does not match the last "
                        "captured model response or an approved deterministic "
                        "tracking disclosure/override."
                    )

        enforcement_outcome = enforcement["outcome"]
        fixture_call_count = enforcement.get("fixture_call_count", 0)
        function_invoked = enforcement.get("function_invoked")
        if fixture_call_count is not None and (
            isinstance(fixture_call_count, bool)
            or not isinstance(fixture_call_count, int)
            or fixture_call_count < 0
        ):
            raise ValueError(
                f"{scenario_id} fixture_call_count must be a non-negative integer."
            )
        if function_invoked is not None and not isinstance(function_invoked, bool):
            raise ValueError(
                f"{scenario_id} function_invoked must be boolean when supplied."
            )
        if function_invoked is True and not fixture_call_count:
            raise ValueError(
                f"{scenario_id} records function_invoked without a fixture call."
            )
        if function_invoked is False and fixture_call_count:
            raise ValueError(
                f"{scenario_id} records fixture calls while function_invoked is false."
            )
        if not record["tool_calls"] and (
            fixture_call_count or function_invoked is True
        ):
            raise ValueError(
                f"{scenario_id} Python execution cannot be recorded without a model tool call."
            )
        if record["tool_calls"] and enforcement_outcome == "not_applicable_no_tool_expected":
            raise ValueError(
                f"{scenario_id} has a model tool call but Python enforcement says no tool was applicable."
            )
        if enforcement_outcome == "allowed_and_executed" and not (
            record["tool_calls"]
            and function_invoked is True
            and fixture_call_count > 0
            and enforcement.get("executor_success") is True
        ):
            raise ValueError(
                f"{scenario_id} allowed_and_executed outcome lacks matching model and fixture execution evidence."
            )
        if (
            enforcement.get("executor_success") is True
            and enforcement_outcome not in {
                "allowed_and_executed",
                "allowed_without_fixture_call",
            }
        ):
            raise ValueError(
                f"{scenario_id} executor_success conflicts with its enforcement outcome."
            )
        if (
            enforcement_outcome == "unexpected_tool_call_blocked"
            and not record["tool_calls"]
        ):
            raise ValueError(
                f"{scenario_id} unexpected-tool enforcement requires a captured model tool call."
            )

        # Validate each recorded model function-call object.
        for call_index, call in enumerate(record["tool_calls"]):
            if not isinstance(call, dict):
                raise ValueError(
                    f"{scenario_id} tool call {call_index} must be a dictionary."
                )
            if not isinstance(call.get("tool_name"), str) or not call["tool_name"]:
                raise ValueError(
                    f"{scenario_id} tool call {call_index} has an invalid tool_name."
                )
            if not isinstance(call.get("call_id"), str) or not call["call_id"]:
                raise ValueError(
                    f"{scenario_id} tool call {call_index} has an invalid call_id."
                )
            if not isinstance(call.get("arguments"), (str, dict)):
                raise ValueError(
                    f"{scenario_id} tool call {call_index} has invalid arguments."
                )

        if record["final_response"] is not None and not isinstance(
            record["final_response"], str
        ):
            raise ValueError(f"{scenario_id} final_response must be text or None.")

        if not isinstance(enforcement.get("outcome"), str) or not enforcement["outcome"]:
            raise ValueError(
                f"{scenario_id} must have a non-empty Python enforcement outcome."
            )

        # Rubric maxima: 1, 1, 2, 2, 2, 1 (total 9).
        score_limits = {
            "intent_and_decision": 1,
            "tool_use_decision": 1,
            "evidence_use": 2,
            "uncertainty_and_qualification": 2,
            "prohibited_claim_avoidance": 2,
            "clarification_or_refusal": 1,
        }
        component_values = [
            scores[field] for field in score_limits
        ]
        if all(value is None for value in component_values):
            if scores["applicable_maximum"] is not None or scores["earned"] is not None:
                raise ValueError(
                    f"{scenario_id} pending scores must have null totals."
                )
        else:
            if any(value is None for value in component_values):
                raise ValueError(
                    f"{scenario_id} scores must be fully pending or fully evaluated."
                )
            applicable_maximum = 0
            earned = 0
            for field, maximum in score_limits.items():
                value = scores[field]
                if value == "N/A":
                    continue
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                    or value > maximum
                ):
                    raise ValueError(
                        f"{scenario_id} score {field} must be 0..{maximum} or N/A."
                    )
                applicable_maximum += maximum
                earned += value
            if (
                isinstance(scores["applicable_maximum"], bool)
                or scores["applicable_maximum"] != applicable_maximum
                or isinstance(scores["earned"], bool)
                or scores["earned"] != earned
            ):
                raise ValueError(
                    f"{scenario_id} score totals do not match the rubric components."
                )

        # Keep model decisions and Python enforcement as separate fields.
        if "actual_model_decision" in enforcement:
            raise ValueError(
                f"{scenario_id} must not store the model decision "
                "inside python_enforcement."
            )

    if actual_ids != expected_ids:
        raise ValueError(
            "Evaluation record IDs must contain every approved scenario "
            "exactly once and in the approved order."
        )

    return {
        "valid": True,
        "record_count": len(records),
        "scenario_ids": actual_ids,
    }

# ---------------------------------------------------------------------
# ask_openai tool-execution recording adapter
# ---------------------------------------------------------------------

@contextmanager
def isolated_security_probe_executor():
    """Yield a fixture provider without modifying the production registry."""
    provider = ScenarioFixtureTrackingProvider("recent_in_transit")
    yield provider



class OfflineScriptedResponses:
    """Return predetermined responses without contacting an API."""

    def __init__(self, responses):
        self._responses = iter(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(to_json_safe(kwargs))
        return next(self._responses)


class OfflineScriptedOpenAIClient:
    """Minimal Responses API-compatible offline client."""

    def __init__(self, responses):
        self.responses = OfflineScriptedResponses(responses)


def _format_offline_scenario_input(specification):
    """
    Include approved prior-message context without creating a database
    conversation or modifying the scenario specification.
    """
    context = specification.get("conversation_context", [])

    if not context:
        return specification["user_input"]

    lines = ["Previous conversation:"]

    for message in context:
        role = str(message.get("role", "user")).capitalize()
        content = str(message.get("content", ""))
        lines.append(f"{role}: {content}")

    lines.extend([
        "",
        "Current user message:",
        specification["user_input"],
    ])

    return "\n".join(lines)


def _resolve_offline_consignment_number(specification):
    """
    Resolve the single synthetic consignment number in the current
    message or approved conversation context.
    """
    source_texts = [specification["user_input"]]

    for message in specification.get("conversation_context", []):
        source_texts.append(str(message.get("content", "")))

    matches = []

    for source_text in source_texts:
        for value in re.findall(
            r"\b[A-Z0-9]{13}\b",
            source_text.upper(),
        ):
            if value not in matches:
                matches.append(value)

    if len(matches) != 1:
        raise ValueError(
            f"{specification['id']} requires exactly one unambiguous "
            "synthetic consignment number for fixture execution."
        )

    return matches[0]


def _make_offline_response(response_id, output, output_text=""):
    return SimpleNamespace(
        id=response_id,
        output=output,
        output_text=output_text,
    )


def _build_offline_scripted_responses(specification):
    """
    Create deterministic mock model outputs.

    These outputs test application orchestration only. They do not
    represent independent model intent recognition or semantic quality.
    """
    scenario_id = specification["id"]
    fixture_key = specification["fixture_key"]

    requires_tracking_call = (
        fixture_key is not None and scenario_id != "E05"
    )

    if requires_tracking_call:
        consignment_number = _resolve_offline_consignment_number(
            specification
        )

        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({
                "consignment_number": consignment_number,
            }),
            call_id=f"offline_call_{scenario_id}",
        )

        return [
            _make_offline_response(
                f"offline_response_{scenario_id}_tool",
                [function_call],
            ),
            _make_offline_response(
                f"offline_response_{scenario_id}_final",
                [],
                (
                    f"Scripted offline final response for {scenario_id}. "
                    "This response is not a semantic model assessment."
                ),
            ),
        ]

    if scenario_id == "E05":
        final_text = (
            "The previously retrieved synthetic result shows the shipment "
            "was InTransit. Its latest recorded checkpoint was 12 November "
            "2026 at Bengaluru Sorting Office. No delivery date is recorded."
        )
    else:
        final_text = (
            f"Scripted offline no-tool response for {scenario_id}. "
            "This response is not a semantic model assessment."
        )

    return [
        _make_offline_response(
            f"offline_response_{scenario_id}_final",
            [],
            final_text,
        )
    ]


def _extract_recorded_model_tool_calls(recording_client):
    """Extract function calls from all captured Responses API rounds."""
    tool_calls = []

    for api_call in recording_client.responses.calls:
        response = api_call.get("response", {})
        response_id = response.get("id")

        for item in response.get("output", []):
            if (
                isinstance(item, dict)
                and item.get("type") == "function_call"
            ):
                tool_calls.append({
                    "response_id": response_id,
                    "call_id": item.get("call_id"),
                    "tool_name": item.get("name"),
                    "arguments": item.get("arguments"),
                })

    return tool_calls

def run_real_model_scenario(
    specification,
    model_identifier,
    recording_client,
):
    """
    Execute one approved real-model scenario.

    Uses the supplied recording client and synthetic tracking fixtures.
    The real Python executor remains active for expected tracking calls.
    Unexpected tracking calls are blocked for no-tool scenarios.

    This function does not create an API client, select credentials,
    or independently authorize live API usage.
    """
    from ai_tools.openai_agent import ask_openai

    if not isinstance(specification, dict):
        raise TypeError("A scenario specification dictionary is required.")

    if not isinstance(model_identifier, str) or not model_identifier.strip():
        raise ValueError("A non-empty model identifier is required.")

    if not isinstance(recording_client, RecordingOpenAIClient):
        raise TypeError("A RecordingOpenAIClient is required.")

    scenario_id = specification.get("id")
    if not isinstance(scenario_id, str) or not scenario_id:
        raise ValueError("The scenario specification requires an ID.")

    if scenario_id in {"F07", "F08", "F09", "F10"}:
        raise ValueError(
            "F07-F10 are direct Python security probes, not model scenarios."
        )

    fixture = resolve_scenario_fixture(scenario_id)

    record = create_evaluation_record(
        scenario_id=scenario_id,
        user_input=specification["user_input"],
        model_identifier=model_identifier.strip(),
        expected_decision=specification["expected_decision"],
        synthetic_fixture=fixture,
    )

    record["conversation_context"] = copy.deepcopy(
        specification.get("conversation_context", [])
    )
    record["model_api_calls"] = []

    record["evaluator_notes"].append(
        "Real-model response captured for later evaluation. "
        "Numerical semantic scores remain pending."
    )

    requires_tracking_call = (
        specification.get("fixture_key") is not None
        and scenario_id != "E05"
    )

    scenario_input = specification["user_input"]
    api_call_start = len(recording_client.responses.calls)


    try:
        if requires_tracking_call:
            provider = ScenarioFixtureTrackingProvider(
                specification["fixture_key"]
            )
            final_response = ask_openai(
                scenario_input,
                client=recording_client,
                model=model_identifier.strip(),
                tool_executor=create_fixture_tool_executor(record, provider),
                conversation_context_messages=copy.deepcopy(
                    specification.get("conversation_context", [])
                ),
            )

            if not provider.calls:
                record["python_enforcement"] = {
                    "outcome": "expected_tool_call_not_made",
                    "details": (
                        "The scenario expected a fixture-backed "
                        "tracking call, but the fixture was not invoked."
                    ),
                }
                record["evaluator_notes"].append(
                    "Expected tracking fixture was not invoked. "
                    "This is not a passing tool-selection result."
                )

        else:
            final_response = ask_openai(
                scenario_input,
                client=recording_client,
                model=model_identifier.strip(),
                tool_executor=create_blocking_tool_executor(record, "real-model"),
                conversation_context_messages=copy.deepcopy(
                    specification.get("conversation_context", [])
                ),
            )

            if record["python_enforcement"]["outcome"] == "not_evaluated":
                record["python_enforcement"] = {
                    "outcome": "not_applicable_no_tool_expected",
                    "details": None,
                }

        record["final_response"] = final_response

    except Exception as exc:
        record["errors"].append({
            "stage": "scenario_execution",
            "error_type": type(exc).__name__,
            "message": str(exc),
        })
        record["evaluator_notes"].append(
            "Scenario execution encountered an error. "
            "This record must not be treated as a pass."
        )

    finally:
        # Capture all attempts, including failed API requests, after
        # the executor context managers have restored their originals.
        scenario_api_calls = copy.deepcopy(
            recording_client.responses.calls[api_call_start:]
        )

        record["model_api_calls"] = scenario_api_calls

        scenario_recording_client = SimpleNamespace(
            responses=SimpleNamespace(calls=scenario_api_calls)
        )

        record["tool_calls"] = _extract_recorded_model_tool_calls(
            scenario_recording_client
        )

        if record["tool_calls"]:
            record["actual_model_decision"] = "tool_call"
            if not requires_tracking_call and record["python_enforcement"]["outcome"] in {
                "not_evaluated", "not_applicable_no_tool_expected"
            }:
                record["python_enforcement"] = {
                    "outcome": "unexpected_tool_call_blocked",
                    "tool_name": record["tool_calls"][0].get("tool_name"),
                    "caller_role": "guest",
                    "registered_tool": False,
                    "argument_keys": ["consignment_number"],
                    "executor_success": False,
                    "function_invoked": False,
                    "fixture_call_count": 0,
                    "error_category": "unexpected_tool_call",
                    "details": "The agent's deterministic candidate gate blocked the unexpected tracking call before the executor was invoked.",
                }
                record["errors"].append({
                    "stage": "tool_selection",
                    "error_type": "UnexpectedToolCall",
                    "message": "The scenario unexpectedly requested a tool. The deterministic Python candidate gate blocked execution.",
                })
        elif any(
            "response" in call
            for call in scenario_api_calls
        ):
            record["actual_model_decision"] = "no_tool"
        else:
            record["actual_model_decision"] = None

    return record

def _run_offline_security_probe(record, specification):
    """Run the real Python enforcement boundary for F07-F10."""
    scenario_id = specification["id"]
    valid_number = "AB123456789IN"

    user_context = None
    assistant_config = None

    if scenario_id == "F07":
        tool_name = "track_india_post"
        arguments = {
            "consignment_number": valid_number,
            "user_id": 123,
        }

    elif scenario_id == "F08":
        tool_name = "unknown_tool"
        arguments = {
            "consignment_number": valid_number,
        }

    elif scenario_id == "F09":
        tool_name = "track_india_post"
        arguments = {
            "consignment_number": valid_number,
        }
        assistant_config = {
            "tools": {
                "track_india_post": {
                    "enabled": True,
                    "permissions": {
                        "guest": False,
                        "registered_user": True,
                    },
                }
            }
        }

    elif scenario_id == "F10":
        tool_name = "track_india_post"
        arguments = {
            "consignment_number": valid_number,
        }
        user_context = {"user_id": 123}
        assistant_config = {
            "tools": {
                "track_india_post": {
                    "enabled": True,
                    "permissions": {
                        "guest": True,
                        "registered_user": False,
                    },
                }
            }
        }

    else:
        raise ValueError(
            f"Unsupported Python security probe: {scenario_id}"
        )

    record["actual_model_decision"] = (
        "not_applicable_python_boundary"
    )

    record["evaluator_notes"].append(
        "Direct Python security-boundary probe; no model call was made."
    )

    with isolated_security_probe_executor() as provider:
        result = execute_tool_with_recording(
            record=record,
            tool_name=tool_name,
            arguments=arguments,
            provider=provider,
            user_context=user_context,
            assistant_config=assistant_config,
            tool_registry={"track_india_post": provider.track_india_post},
        )

        if provider.calls:
            raise RuntimeError(
                f"{scenario_id} unexpectedly invoked the tracking fixture."
            )

    record["tool_calls"] = []
    record["final_response"] = None

    return result


def run_offline_evaluation(
    model_identifier="offline-scripted-mock-v1",
):
    """
    Execute all 39 approved scenarios using scripted mock model
    responses and synthetic tracking fixtures.

    No live OpenAI API, AfterShip provider, real consignment lookup,
    or conversation database persistence is used.

    Model semantic scores intentionally remain pending.
    """
    from ai_tools.openai_agent import ask_openai
    
    if (
        not isinstance(model_identifier, str)
        or not model_identifier.strip()
    ):
        raise ValueError("A non-empty model identifier is required.")

    validate_scenario_specifications()
    validate_synthetic_tracking_fixtures()

    specifications = load_scenario_specifications()
    records = []

    for specification in specifications:
        scenario_id = specification["id"]
        fixture = resolve_scenario_fixture(scenario_id)

        record = create_evaluation_record(
            scenario_id=scenario_id,
            user_input=specification["user_input"],
            model_identifier=model_identifier,
            expected_decision=specification["expected_decision"],
            synthetic_fixture=fixture,
        )

        record["conversation_context"] = copy.deepcopy(
            specification.get("conversation_context", [])
        )
        record["model_api_calls"] = []

        record["evaluator_notes"].append(
            "Scripted mock output; real-model semantic evaluation "
            "has not been performed."
        )

        try:
            if scenario_id in {"F07", "F08", "F09", "F10"}:
                _run_offline_security_probe(record, specification)

            else:
                scripted_responses = _build_offline_scripted_responses(
                    specification
                )

                mock_client = OfflineScriptedOpenAIClient(
                    scripted_responses
                )

                recording_client = RecordingOpenAIClient(mock_client)

                scenario_input = specification["user_input"]
                scenario_context = copy.deepcopy(
                    specification.get("conversation_context", [])
                )

                requires_tracking_call = (
                    specification["fixture_key"] is not None
                    and scenario_id != "E05"
                )

                if requires_tracking_call:
                    fixture_key = specification["fixture_key"]
                    provider = ScenarioFixtureTrackingProvider(fixture_key)
                    final_response = ask_openai(
                        scenario_input,
                        client=recording_client,
                        model=model_identifier,
                        tool_executor=create_fixture_tool_executor(record, provider),
                        conversation_context_messages=scenario_context,
                    )

                    if not provider.calls:
                        record["errors"].append({
                            "stage": "tool_execution",
                            "error_type": "ExpectedToolCallMissing",
                            "message": (
                                "The scripted scenario did not execute "
                                "its expected fixture-backed tracking call."
                            ),
                        })

                else:
                    final_response = ask_openai(
                        scenario_input,
                        client=recording_client,
                        model=model_identifier,
                        tool_executor=create_blocking_tool_executor(record, "offline"),
                        conversation_context_messages=scenario_context,
                    )

                    if record["python_enforcement"]["outcome"] == "not_evaluated":
                        record["python_enforcement"] = {
                            "outcome": "not_applicable_no_tool_expected",
                            "details": None,
                        }

                record["final_response"] = final_response

                record["model_api_calls"] = copy.deepcopy(
                    recording_client.responses.calls
                )

                record["tool_calls"] = (
                    _extract_recorded_model_tool_calls(recording_client)
                )

                record["actual_model_decision"] = (
                    "tool_call" if record["tool_calls"] else "no_tool"
                )

                if scenario_id == "E05":
                    record["evaluator_notes"].append(
                        "E05 supplies a prior synthetic tracking result in "
                        "assistant-authored conversation context. The current "
                        "request is summary-only, so no new tracking call is expected."
                    )

        except Exception as exc:
            record["errors"].append({
                "stage": "scenario_execution",
                "error_type": type(exc).__name__,
                "message": str(exc),
            })

            record["evaluator_notes"].append(
                "Scenario execution encountered an error. "
                "This record must not be treated as a pass."
            )

        records.append(record)

    validation = validate_evaluation_records(records)
    report_json = serialize_evaluation_records(records)

    error_count = sum(
        len(record["errors"])
        for record in records
    )

    fixture_call_count = sum(
        record["python_enforcement"].get("fixture_call_count", 0)
        for record in records
    )

    offline_api_calls = [
        api_call
        for record in records
        for api_call in record.get("model_api_calls", [])
    ]
    model_api_call_count = len(offline_api_calls)
    api_request_forwarded_count = sum(
        api_call.get("forwarded") is True
        for api_call in offline_api_calls
    )
    api_request_failed_count = sum(
        api_call.get("forwarded") is True
        and api_call.get("failed") is True
        for api_call in offline_api_calls
    )
    api_request_budget_blocked_count = sum(
        api_call.get("budget_blocked") is True
        for api_call in offline_api_calls
    )

    return {
        "mode": "offline_mock",
        "model_identifier": model_identifier,
        "validation": validation,
        "summary": {
            "scenario_count": len(records),
            "model_api_call_count": model_api_call_count,
            "api_request_attempt_count": model_api_call_count,
            "api_request_forwarded_count": api_request_forwarded_count,
            "api_request_failed_count": api_request_failed_count,
            "api_request_budget_blocked_count": api_request_budget_blocked_count,
            "fixture_call_count": fixture_call_count,
            "error_count": error_count,
            "semantic_scores_pending": True,
            "semantic_evaluation_passed": False,
            "report_sanitization": (
                "Defense-in-depth pattern and field-name redaction; "
                "not a guarantee of complete sensitive-data detection."
            ),
            "cost_control_note": (
                "The API request cap limits request count, not monetary spend."
            ),
        },
        "records": records,
        "report_json": report_json,
    }

def run_real_model_evaluation(
    model_identifier,
    timeout_seconds=30,
    confirm_live_api_usage=False,
    recording_client=None,
    max_api_requests=70,
):
    """
    Execute all 39 approved scenarios using a real model client
    and synthetic tracking fixtures.

    A recording client may be injected for offline tests.
    Otherwise, a real OpenAI client is constructed only after
    explicit live-API confirmation.

    F07-F10 remain direct Python security probes and do not
    make model calls.

    No AfterShip provider or production conversation database
    is used. Semantic scores remain pending.
    """
    config = validate_real_model_evaluation_config(
        model_identifier=model_identifier,
        timeout_seconds=timeout_seconds,
        confirm_live_api_usage=confirm_live_api_usage,
    )

    selected_model = config["model_identifier"]
    # Validate the request cap before reading credentials or constructing a client.
    request_budget = APIRequestBudget(max_api_requests)

    if recording_client is None:
        recording_client = build_real_model_recording_client(
            selected_model,
            timeout_seconds=config["timeout_seconds"],
        )

    if not isinstance(recording_client, RecordingOpenAIClient):
        raise TypeError(
            "A RecordingOpenAIClient is required."
        )

    recording_client.responses.request_budget = request_budget

    specifications = load_scenario_specifications()
    records = []

    security_probe_ids = {"F07", "F08", "F09", "F10"}

    for specification in specifications:
        scenario_id = specification["id"]

        if scenario_id not in security_probe_ids:
            if request_budget.exhausted:
                fixture = resolve_scenario_fixture(scenario_id)
                record = create_evaluation_record(
                    scenario_id=scenario_id,
                    user_input=specification["user_input"],
                    model_identifier=selected_model,
                    expected_decision=specification["expected_decision"],
                    synthetic_fixture=fixture,
                )
                record["actual_model_decision"] = None
                record["python_enforcement"] = {
                    "outcome": "not_run_api_budget_exhausted",
                    "details": "No API request was attempted for this scenario.",
                }
                record["evaluator_notes"].append(
                    "Scenario not run because the evaluation API-request "
                    "budget had already been exhausted."
                )
                record["model_api_calls"] = []
                record["conversation_context"] = copy.deepcopy(
                    specification.get("conversation_context", [])
                )
            else:
                record = run_real_model_scenario(
                    specification=specification,
                    model_identifier=selected_model,
                    recording_client=recording_client,
                )

        else:
            fixture = resolve_scenario_fixture(scenario_id)

            record = create_evaluation_record(
                scenario_id=scenario_id,
                user_input=specification["user_input"],
                model_identifier=selected_model,
                expected_decision=specification["expected_decision"],
                synthetic_fixture=fixture,
            )

            record["conversation_context"] = copy.deepcopy(
                specification.get("conversation_context", [])
            )
            record["model_api_calls"] = []

            try:
                _run_offline_security_probe(
                    record,
                    specification,
                )

            except Exception as exc:
                record["errors"].append({
                    "stage": "python_security_probe",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                })

                record["evaluator_notes"].append(
                    "Python security probe encountered an error. "
                    "This record must not be treated as a pass."
                )

                if (
                    record["python_enforcement"].get("outcome")
                    == "not_evaluated"
                ):
                    record["python_enforcement"] = {
                        "outcome": "security_probe_failed",
                        "details": (
                            "The Python security probe did not "
                            "complete successfully."
                        ),
                    }

            record["scores"] = {
                key: None
                for key in record["scores"]
            }

        records.append(record)

    validation = validate_evaluation_records(records)
    report_json = serialize_evaluation_records(records)

    all_api_calls = [
        api_call
        for record in records
        for api_call in record.get("model_api_calls", [])
    ]
    api_request_attempt_count = len(all_api_calls)
    api_request_forwarded_count = sum(
        api_call.get("forwarded") is True
        for api_call in all_api_calls
    )
    api_request_failed_count = sum(
        api_call.get("forwarded") is True
        and api_call.get("failed") is True
        for api_call in all_api_calls
    )
    api_request_budget_blocked_count = sum(
        api_call.get("budget_blocked") is True
        for api_call in all_api_calls
    )
    api_request_succeeded_count = sum(
        api_call.get("forwarded") is True
        and api_call.get("failed") is not True
        and "response" in api_call
        for api_call in all_api_calls
    )

    skipped_scenario_count = sum(
        record["python_enforcement"].get("outcome")
        == "not_run_api_budget_exhausted"
        for record in records
    )
    scenario_error_count = sum(bool(record["errors"]) for record in records)
    model_scenarios_attempted_count = 35 - skipped_scenario_count
    if skipped_scenario_count:
        evaluation_status = "incomplete_budget_exhausted"
    elif scenario_error_count:
        evaluation_status = "completed_with_errors"
    else:
        evaluation_status = "completed_scores_pending"

    fixture_call_count = sum(
        record["python_enforcement"].get(
            "fixture_call_count",
            0,
        )
        for record in records
    )

    error_count = sum(
        len(record["errors"])
        for record in records
    )

    sanitized_records = sanitize_evaluation_data(records)

    return {
        "mode": "real_model",
        "model_identifier": selected_model,
        "validation": validation,
        "summary": {
            "scenario_count": len(records),
            "model_scenario_count": 35,
            "python_security_probe_count": 4,
            "evaluation_status": evaluation_status,
            "model_scenarios_attempted_count": model_scenarios_attempted_count,
            "model_scenarios_skipped_count": skipped_scenario_count,
            "model_api_call_count": api_request_forwarded_count,
            "api_request_attempt_count": api_request_attempt_count,
            "api_request_forwarded_count": api_request_forwarded_count,
            "api_request_failed_count": api_request_failed_count,
            "api_request_succeeded_count": api_request_succeeded_count,
            "api_request_budget_blocked_count": api_request_budget_blocked_count,
            "api_request_budget": request_budget.max_requests,
            "api_requests_used": request_budget.requests_used,
            "api_request_budget_exhausted": request_budget.exhausted,
            "fixture_call_count": fixture_call_count,
            "error_count": error_count,
            "scenario_error_count": scenario_error_count,
            "semantic_scores_pending": True,
            "semantic_evaluation_passed": False,
            "report_sanitization": (
                "Export applies recursive sensitive-field and pattern-based "
                "redaction. This is defense in depth, not comprehensive DLP."
            ),
            "in_memory_data_handling": (
                "During execution, raw model requests/responses and scenario "
                "content exist in memory. A caller-held RecordingOpenAIClient "
                "retains raw responses.calls after the run. Returned records "
                "and report_json are sanitized copies; sanitization is not "
                "guaranteed to detect every sensitive value."
            ),
            "cost_control_note": (
                "The API request cap limits request count, not monetary spend."
            ),
        },
        "records": sanitized_records,
        "report_json": report_json,
    }


# ---------------------------------------------------------------------
# Report serialization
# ---------------------------------------------------------------------

def serialize_evaluation_records(records):
    """
    Serialize either one evaluation record or a complete report.

    A single record is useful for isolated inspection.
    A complete report must contain all 39 approved scenarios.
    """
    if not isinstance(records, list):
        raise TypeError("Evaluation records must be supplied as a list.")

    if len(records) == 1:
        record = records[0]

        if not isinstance(record, dict):
            raise TypeError("Evaluation record must be a dictionary.")

        if not isinstance(record.get("scenario_id"), str):
            raise ValueError("Evaluation record requires a scenario_id.")

        if not isinstance(record.get("python_enforcement"), dict):
            raise ValueError(
                "Evaluation record requires python_enforcement."
            )

        if not isinstance(record.get("python_enforcement", {}).get("outcome"), str):
            raise ValueError(
                "Evaluation record requires a Python enforcement outcome."
            )

    else:
        validate_evaluation_records(records)

    serialized = json.dumps(
        sanitize_evaluation_data(records),
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    )

    restored = json.loads(serialized)

    if not isinstance(restored, list) or len(restored) != len(records):
        raise ValueError(
            "Serialized evaluation data failed its integrity check."
        )

    return serialized
