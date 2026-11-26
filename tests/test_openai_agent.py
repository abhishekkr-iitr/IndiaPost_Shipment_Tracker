
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    RateLimitError,
)

from ai_tools.openai_agent import (
    ask_openai,
    OpenAIAgentError
)


class MockResponses:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.responses)


class MockOpenAIClient:
    def __init__(self, responses):
        self.responses = MockResponses(responses)


def make_response(response_id, output, output_text=""):
    return SimpleNamespace(
        id=response_id,
        output=output,
        output_text=output_text
    )


class TestOpenAIAgent(unittest.TestCase):

    def test_normal_text_response(self):
        client = MockOpenAIClient([
            make_response(
                "response_1",
                [],
                "How can I help with your shipment?"
            )
        ])

        result = ask_openai(
            "Hello",
            client=client,
            model="mock-model"
        )

        self.assertEqual(
            result,
            "How can I help with your shipment?"
        )

        self.assertEqual(len(client.responses.calls), 1)

    @patch("ai_tools.openai_agent.execute_tool")
    def test_tracking_function_call(self, mock_execute_tool):
        mock_execute_tool.return_value = {
            "success": True,
            "data": {
                "status": "In Transit"
            }
        }

        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({
                "consignment_number": "AB123456789IN"
            }),
            call_id="call_123"
        )

        client = MockOpenAIClient([
            make_response(
                "response_1",
                [function_call]
            ),
            make_response(
                "response_2",
                [],
                "Your shipment is in transit."
            )
        ])

        result = ask_openai(
            "Track AB123456789IN",
            client=client,
            model="mock-model"
        )

        self.assertEqual(
            result,
            "Your shipment is in transit."
        )

        mock_execute_tool.assert_called_once_with(
            "track_india_post",
            function_call.arguments
        )

        self.assertEqual(len(client.responses.calls), 2)

        second_call = client.responses.calls[1]

        self.assertEqual(
            second_call["previous_response_id"],
            "response_1"
        )

        tool_output = second_call["input"][0]

        self.assertEqual(
            tool_output["type"],
            "function_call_output"
        )

        self.assertEqual(
            tool_output["call_id"],
            "call_123"
        )

        returned_data = json.loads(tool_output["output"])

        self.assertTrue(returned_data["success"])

    def test_maximum_tool_rounds(self):
        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({
                "consignment_number": "AB123456789IN"
            }),
            call_id="call_123"
        )

        client = MockOpenAIClient([
            make_response(
                "response_1",
                [function_call]
            ),
            make_response(
                "response_2",
                [function_call]
            )
        ])

        with patch(
            "ai_tools.openai_agent.execute_tool",
            return_value={"success": True}
        ):
            with self.assertRaises(OpenAIAgentError):
                ask_openai(
                    "Track AB123456789IN",
                    client=client,
                    model="mock-model",
                    max_tool_rounds=1
                )

        self.assertEqual(len(client.responses.calls), 2)

    def test_empty_user_message(self):
        with self.assertRaises(ValueError):
            ask_openai(
                "",
                client=MockOpenAIClient([]),
                model="mock-model"
            )

    def test_missing_model_configuration(self):
        with patch.dict(
            "os.environ",
            {},
            clear=True
        ):
            with self.assertRaises(OpenAIAgentError):
                ask_openai(
                    "Hello",
                    client=MockOpenAIClient([]),
                    model=None
                )

    def test_missing_api_key(self):
        with patch.dict(
            "os.environ",
            {},
            clear=True
        ):
            with self.assertRaises(OpenAIAgentError):
                ask_openai(
                    "Hello",
                    client=None,
                    model="mock-model"
                )


    @patch("ai_tools.openai_agent.execute_tool")
    def test_multiple_function_calls(self, mock_execute_tool):
        mock_execute_tool.side_effect = [
            {
                "success": True,
                "data": {"status": "In Transit"}
            },
            {
                "success": True,
                "data": {"status": "Delivered"}
            }
        ]

        first_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({
                "consignment_number": "AB123456789IN"
            }),
            call_id="call_001"
        )

        second_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({
                "consignment_number": "CD123456789IN"
            }),
            call_id="call_002"
        )

        client = MockOpenAIClient([
            make_response(
                "response_1",
                [first_call, second_call]
            ),
            make_response(
                "response_2",
                [],
                "Both shipment results have been retrieved."
            )
        ])

        result = ask_openai(
            "Track AB123456789IN and CD123456789IN.",
            client=client,
            model="mock-model"
        )

        self.assertEqual(
            result,
            "I found more than one possible consignment number in your "
            "message. Which one would you like me to track?"
        )
        self.assertEqual(mock_execute_tool.call_count, 0)
        # The deterministic gate returns clarification without asking the
        # model to continue after its rejected ambiguous tool calls.
        self.assertEqual(len(client.responses.calls), 1)


    @patch("ai_tools.openai_agent.execute_tool")
    def test_stale_checkpoint_disclosure_is_appended_deterministically(
        self, mock_execute_tool
    ):
        from datetime import date
        mock_execute_tool.return_value = {
            "success": True,
            "data": {
                "current_status": "InTransit",
                "checkpoints": [{
                    "date": "2026-11-02T10:00:00+05:30",
                    "location": "Delhi Sorting Office",
                    "status": "InTransit",
                    "message": "Item processed",
                }],
            },
        }
        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({"consignment_number": "AB123456789IN"}),
            call_id="call_stale_disclosure",
        )
        client = MockOpenAIClient([
            make_response("response_stale_tool", [function_call]),
            make_response(
                "response_stale_final",
                [],
                "Your shipment is currently in transit. The latest checkpoint was recorded on 2 November 2026.",
            ),
        ])

        with patch(
            "ai_tools.openai_agent._india_local_date",
            return_value=date(2026, 11, 15),
        ):
            result = ask_openai(
                "Track AB123456789IN",
                client=client,
                model="mock-model",
            )

        self.assertTrue(result.startswith("The latest recorded checkpoint shows In Transit"))
        self.assertIn("Delhi Sorting Office", result)
        self.assertIn("2 November 2026", result)
        self.assertIn("13 days old", result)
        self.assertIn("may be stale", result)
        self.assertIn("does not confirm the shipment's current location or current status", result)
        self.assertNotIn("currently in transit", result.lower())

    @patch("ai_tools.openai_agent.execute_tool")
    def test_unknown_status_replaces_speculative_model_response(
        self, mock_execute_tool
    ):
        mock_execute_tool.return_value = {
            "success": True,
            "data": {
                "current_status": "Unknown",
                "status_message": None,
                "origin": None,
                "destination": None,
                "pickup_date": None,
                "delivery_date": None,
                "checkpoints": [],
            },
        }
        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({"consignment_number": "AB123456789IN"}),
            call_id="call_unknown_status",
        )
        client = MockOpenAIClient([
            make_response("response_unknown_tool", [function_call]),
            make_response(
                "response_unknown_final",
                [],
                "There are no updates in the India Post system, so the parcel "
                "may not be registered.",
            ),
        ])

        result = ask_openai(
            "Track AB123456789IN",
            client=client,
            model="mock-model",
        )

        self.assertTrue(result.startswith("Unknown-status note:"))
        self.assertIn("unknown or unrecognized status ('Unknown')", result)
        self.assertIn("do not establish the shipment's current status", result)
        self.assertIn("does not establish that the consignment is unregistered", result)
        self.assertNotIn("no updates in the India Post system", result.lower())
        self.assertNotIn("not registered", result.lower())
        self.assertIn("does not establish that the consignment is unregistered, lost", result.lower())
        self.assertIn("or has not moved", result.lower())
        mock_execute_tool.assert_called_once()
        self.assertEqual(len(client.responses.calls), 2)

    @patch("ai_tools.openai_agent.execute_tool")
    def test_unknown_status_with_checkpoint_evidence_is_not_overridden(
        self, mock_execute_tool
    ):
        mock_execute_tool.return_value = {
            "success": True,
            "data": {
                "current_status": "Unknown",
                "status_message": None,
                "checkpoints": [{
                    "date": "2026-11-14T10:00:00+05:30",
                    "location": "Bengaluru Sorting Office",
                    "status": "InTransit",
                    "message": "Item processed",
                }],
            },
        }
        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({"consignment_number": "AB123456789IN"}),
            call_id="call_unknown_with_checkpoint",
        )
        client = MockOpenAIClient([
            make_response("response_unknown_checkpoint_tool", [function_call]),
            make_response(
                "response_unknown_checkpoint_final",
                [],
                "The current status field is unknown, but the latest checkpoint "
                "records In Transit at Bengaluru Sorting Office.",
            ),
        ])

        result = ask_openai(
            "Track AB123456789IN",
            client=client,
            model="mock-model",
        )

        self.assertTrue(result.startswith("Unknown-status note:"))
        self.assertIn("latest available checkpoint records status InTransit", result)
        self.assertIn("Bengaluru Sorting Office", result)
        self.assertIn("available records do not establish the shipment's current status", result)
        self.assertNotIn("currently in transit", result.lower())

    @patch("ai_tools.openai_agent.execute_tool")
    def test_conflicting_tracking_status_disclosure_is_appended(
        self, mock_execute_tool
    ):
        mock_execute_tool.return_value = {
            "success": True,
            "data": {
                "current_status": "InTransit",
                "checkpoints": [{
                    "date": "2026-11-13T10:00:00+05:30",
                    "location": "Bengaluru Sorting Office",
                    "status": "Delivered",
                    "message": "Checkpoint reports Delivered",
                }],
            },
        }
        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({"consignment_number": "AB123456789IN"}),
            call_id="call_conflict_disclosure",
        )
        client = MockOpenAIClient([
            make_response("response_conflict_tool", [function_call]),
            make_response(
                "response_conflict_final",
                [],
                "The current status is In Transit. A checkpoint says Delivered.",
            ),
        ])

        result = ask_openai(
            "Track AB123456789IN",
            client=client,
            model="mock-model",
        )

        self.assertTrue(result.startswith("Data consistency note:"))
        self.assertNotIn("The current status is In Transit.", result)
        self.assertIn("current-status field reports 'InTransit'", result)
        self.assertIn("checkpoint reports 'Delivered'", result)
        self.assertIn("13 November 2026", result)
        self.assertIn("Bengaluru Sorting Office", result)
        self.assertIn("records conflict", result)
        self.assertIn("does not establish which value is authoritative", result)
        mock_execute_tool.assert_called_once()

    @patch("ai_tools.openai_agent.execute_tool")
    def test_unavailable_tracking_data_disclosure_is_appended(
        self, mock_execute_tool
    ):
        mock_execute_tool.return_value = {
            "success": False,
            "error": "No tracking information available.",
        }
        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({"consignment_number": "AB123456789IN"}),
            call_id="call_unavailable_disclosure",
        )
        client = MockOpenAIClient([
            make_response("response_unavailable_tool", [function_call]),
            make_response(
                "response_unavailable_final",
                [],
                "I could not retrieve the tracking information.",
            ),
        ])

        result = ask_openai(
            "Track AB123456789IN",
            client=client,
            model="mock-model",
        )

        self.assertTrue(result.startswith("Availability note:"))
        self.assertNotIn("might mean", result.lower())
        self.assertNotIn("not been updated in the system", result.lower())
        self.assertNotIn("I could not retrieve the tracking information.", result)
        self.assertIn("returned no tracking information", result)
        self.assertIn("does not establish that the consignment number is invalid", result)
        self.assertIn("that the shipment is lost", result)
        self.assertIn("available result does not establish the shipment's current status", result)
        mock_execute_tool.assert_called_once()

    @patch("ai_tools.openai_agent.execute_tool")
    def test_provider_failure_is_not_misclassified_as_unavailable_data(
        self, mock_execute_tool
    ):
        mock_execute_tool.return_value = {
            "success": False,
            "error": "Tracking provider request failed.",
        }
        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({"consignment_number": "AB123456789IN"}),
            call_id="call_provider_failure",
        )
        client = MockOpenAIClient([
            make_response("response_provider_failure_tool", [function_call]),
            make_response(
                "response_provider_failure_final",
                [],
                "The tracking provider request failed.",
            ),
        ])

        result = ask_openai(
            "Track AB123456789IN",
            client=client,
            model="mock-model",
        )

        self.assertNotIn("Availability note:", result)
        self.assertIn("provider request failed", result.lower())
        mock_execute_tool.assert_called_once()

    @patch("ai_tools.openai_agent.OpenAI")
    def test_client_uses_default_reliability_settings(self, mock_openai):
        with patch.dict(
            "os.environ",
            {"OPENAI_API_KEY": "mock-test-key"},
            clear=True
        ):
            from ai_tools.openai_agent import _get_openai_client

            _get_openai_client()

        mock_openai.assert_called_once_with(
            api_key="mock-test-key",
            timeout=30.0,
            max_retries=2
        )

    @patch("ai_tools.openai_agent.OpenAI")
    def test_client_uses_custom_reliability_settings(self, mock_openai):
        with patch.dict(
            "os.environ",
            {
                "OPENAI_API_KEY": "mock-test-key",
                "OPENAI_TIMEOUT_SECONDS": "45",
                "OPENAI_MAX_RETRIES": "4"
            },
            clear=True
        ):
            from ai_tools.openai_agent import _get_openai_client

            _get_openai_client()

        mock_openai.assert_called_once_with(
            api_key="mock-test-key",
            timeout=45.0,
            max_retries=4
        )


    def test_invalid_timeout_configuration(self):
        with patch.dict(
            "os.environ",
            {
                "OPENAI_API_KEY": "mock-test-key",
                "OPENAI_TIMEOUT_SECONDS": "0"
            },
            clear=True
        ):
            from ai_tools.openai_agent import _get_openai_client

            with self.assertRaises(OpenAIAgentError):
                _get_openai_client()

    def test_invalid_retry_configuration(self):
        with patch.dict(
            "os.environ",
            {
                "OPENAI_API_KEY": "mock-test-key",
                "OPENAI_MAX_RETRIES": "-1"
            },
            clear=True
        ):
            from ai_tools.openai_agent import _get_openai_client

            with self.assertRaises(OpenAIAgentError):
                _get_openai_client()


    @patch("ai_tools.openai_agent.execute_tool")
    def test_registered_user_context_forwarded(self, mock_execute_tool):
        mock_execute_tool.return_value = {
            "success": True,
            "data": {"status": "In Transit"}
        }

        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({
                "consignment_number": "AB123456789IN"
            }),
            call_id="call_user_123"
        )

        client = MockOpenAIClient([
            make_response(
                "response_1",
                [function_call]
            ),
            make_response(
                "response_2",
                [],
                "Your shipment is in transit."
            )
        ])

        trusted_context = {"user_id": 123}

        result = ask_openai(
            "Track AB123456789IN",
            client=client,
            model="mock-model",
            user_context=trusted_context
        )

        self.assertEqual(
            result,
            "Your shipment is in transit."
        )

        mock_execute_tool.assert_called_once_with(
            "track_india_post",
            function_call.arguments,
            user_context=trusted_context
        )

        # The trusted context must not be sent in the model's message.
        self.assertEqual(
            client.responses.calls[0]["input"],
            "Track AB123456789IN"
        )

    @patch("ai_tools.openai_agent.execute_tool")
    def test_guest_request_preserves_original_executor_call(
        self,
        mock_execute_tool
    ):
        mock_execute_tool.return_value = {
            "success": True,
            "data": {"status": "In Transit"}
        }

        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({
                "consignment_number": "AB123456789IN"
            }),
            call_id="call_guest_001"
        )

        client = MockOpenAIClient([
            make_response(
                "response_1",
                [function_call]
            ),
            make_response(
                "response_2",
                [],
                "Your shipment is in transit."
            )
        ])

        result = ask_openai(
            "Track AB123456789IN",
            client=client,
            model="mock-model"
        )

        self.assertEqual(
            result,
            "Your shipment is in transit."
        )

        # Guest requests must retain the original two-argument call.
        mock_execute_tool.assert_called_once_with(
            "track_india_post",
            function_call.arguments
        )


    def test_invalid_user_context_rejected_before_api_call(self):
        client = MockOpenAIClient([])

        with self.assertRaises(ValueError):
            ask_openai(
                "Track AB123456789IN",
                client=client,
                model="mock-model",
                user_context={"user_id": "123"}
            )

        self.assertEqual(len(client.responses.calls), 0)

    @patch("ai_tools.openai_agent.OpenAI")
    def test_non_finite_timeout_configuration_rejected(self, mock_openai):
        for timeout_value in ("nan", "inf", "-inf"):
            with self.subTest(timeout=timeout_value):
                with patch.dict(
                    "os.environ",
                    {
                        "OPENAI_API_KEY": "mock-test-key",
                        "OPENAI_TIMEOUT_SECONDS": timeout_value
                    },
                    clear=True
                ):
                    from ai_tools.openai_agent import _get_openai_client

                    with self.assertRaises(OpenAIAgentError):
                        _get_openai_client()

        mock_openai.assert_not_called()

    @patch("ai_tools.openai_agent.execute_tool")
    def test_resolved_instructions_are_sent_on_initial_and_followup_requests(
        self, mock_execute_tool
    ):
        mock_execute_tool.return_value = {"success": True, "data": {"status": "In Transit"}}
        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({"consignment_number": "AB123456789IN"}),
            call_id="call_instructions",
        )
        client = MockOpenAIClient([
            make_response("response_1", [function_call]),
            make_response("response_2", [], "Tracking result."),
        ])
        custom_config = {
            "instructions": {
                "business": ["Use the Acme business identity."],
                "application": ["Keep replies concise."],
                "user_preferences": ["Prefer English."],
            }
        }

        result = ask_openai(
            "Track AB123456789IN",
            client=client,
            model="mock-model",
            assistant_config=custom_config,
        )

        self.assertEqual(result, "Tracking result.")
        self.assertEqual(len(client.responses.calls), 2)
        first_instructions = client.responses.calls[0]["instructions"]
        second_instructions = client.responses.calls[1]["instructions"]
        self.assertEqual(first_instructions, second_instructions)
        self.assertIn("Use the Acme business identity.", first_instructions)
        self.assertIn("Keep replies concise.", first_instructions)
        self.assertIn("Prefer English.", first_instructions)

    @patch("ai_tools.openai_agent.execute_tool")
    def test_shipment_response_policies_are_sent_on_initial_and_followup_requests(
        self, mock_execute_tool
    ):
        mock_execute_tool.return_value = {
            "success": True,
            "data": {"status": "Delivered"},
        }

        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({
                "consignment_number": "AB123456789IN"
            }),
            call_id="call_shipment_policy",
        )

        client = MockOpenAIClient([
            make_response("response_policy_1", [function_call]),
            make_response("response_policy_2", [], "Shipment status recorded."),
        ])

        custom_config = {
            "business_rules": {
                "shipment_response_policies": {
                    "delivered": {
                        "additional_requirements": [
                            "State that the tracking record reports delivery.",
                            "Do not imply that the recipient personally received the item.",
                        ],
                    },
                },
            },
        }

        result = ask_openai(
            "Track AB123456789IN",
            client=client,
            model="mock-model",
            assistant_config=custom_config,
        )

        self.assertEqual(result, "Shipment status recorded.")
        self.assertEqual(len(client.responses.calls), 2)

        first_instructions = client.responses.calls[0]["instructions"]
        second_instructions = client.responses.calls[1]["instructions"]

        self.assertEqual(first_instructions, second_instructions)
        self.assertIn("SHIPMENT RESPONSE POLICIES", first_instructions)
        self.assertIn("DELIVERED", first_instructions)
        self.assertIn(
            "State that the tracking record reports delivery.",
            first_instructions,
        )
        self.assertIn(
            "Do not imply that the recipient personally received the item.",
            first_instructions,
        )
        self.assertIn("PLATFORM REQUIREMENTS", first_instructions)
        self.assertIn("APPLICATION INSTRUCTIONS", first_instructions)

        mock_execute_tool.assert_called_once_with(
            "track_india_post",
            function_call.arguments,
            assistant_config=custom_config,
        )

    def test_clarification_response_does_not_invoke_tracking_tool(self):
        client = MockOpenAIClient([
            make_response(
                "response_clarification",
                [],
                "Please provide the 13-character consignment number."
            )
        ])
        with patch("ai_tools.openai_agent.execute_tool") as mock_execute:
            result = ask_openai(
                "Please track my shipment",
                client=client,
                model="mock-model",
            )
        self.assertIn("provide", result.lower())
        mock_execute.assert_not_called()

    def test_default_instructions_include_consignment_safety_rules(self):
        client = MockOpenAIClient([
            make_response("response_clarification_rules", [], "Please clarify.")
        ])
        ask_openai(
            "Track my parcel",
            client=client,
            model="mock-model",
        )
        instructions = client.responses.calls[0]["instructions"]
        self.assertIn("one unambiguous consignment number", instructions)
        self.assertIn("multiple possible consignment numbers", instructions)
        self.assertIn("Do not invent, guess, complete", instructions)

    def test_default_instructions_sent_without_new_argument(self):
        client = MockOpenAIClient([
            make_response("response_default_instructions", [], "Hello")
        ])
        self.assertEqual(
            ask_openai("Hello", client=client, model="mock-model"),
            "Hello",
        )
        self.assertIn("PLATFORM REQUIREMENTS", client.responses.calls[0]["instructions"])

    def test_invalid_assistant_config_rejected_before_api_call(self):
        client = MockOpenAIClient([])
        with self.assertRaises(ValueError):
            ask_openai(
                "Hello",
                client=client,
                model="mock-model",
                assistant_config={"instructions": {"platform": ["override"]}},
            )
        self.assertEqual(len(client.responses.calls), 0)

    def test_disabled_tool_is_not_sent_to_model(self):
        client = MockOpenAIClient([
            make_response("response_tools_disabled", [], "I can help with general questions.")
        ])
        result = ask_openai(
            "Hello",
            client=client,
            model="mock-model",
            assistant_config={"tools": {"track_india_post": {"enabled": False}}},
        )
        self.assertEqual(result, "I can help with general questions.")
        self.assertEqual(client.responses.calls[0]["tools"], [])

    @patch("ai_tools.openai_agent.execute_tool")
    def test_enabled_tool_schema_is_sent_on_initial_and_followup_requests(self, mock_execute_tool):
        mock_execute_tool.return_value = {"success": True, "data": {"status": "In Transit"}}
        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({"consignment_number": "AB123456789IN"}),
            call_id="call_tool_config",
        )
        client = MockOpenAIClient([
            make_response("response_tool_config_1", [function_call]),
            make_response("response_tool_config_2", [], "Tracking result."),
        ])
        config = {"tools": {"track_india_post": {"enabled": True}}}
        self.assertEqual(
            ask_openai("Track AB123456789IN", client=client, model="mock-model", assistant_config=config),
            "Tracking result.",
        )
        first_tools = client.responses.calls[0]["tools"]
        second_tools = client.responses.calls[1]["tools"]
        self.assertEqual(first_tools, second_tools)
        self.assertEqual([tool["name"] for tool in first_tools], ["track_india_post"])

    def test_unknown_tool_configuration_rejected_before_api_call(self):
        client = MockOpenAIClient([])
        with self.assertRaises(ValueError):
            ask_openai(
                "Hello",
                client=client,
                model="mock-model",
                assistant_config={"tools": {"delete_database": {"enabled": True}}},
            )
        self.assertEqual(client.responses.calls, [])


    @patch("ai_tools.openai_agent.execute_tool")
    def test_trusted_assistant_config_is_forwarded_to_executor(self, mock_execute_tool):
        mock_execute_tool.return_value = {"success": True}
        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({"consignment_number": "AB123456789IN"}),
            call_id="call_config_123",
        )
        client = MockOpenAIClient([
            make_response("response_1", [function_call]),
            make_response("response_2", [], "Tracking completed."),
        ])
        config = {
            "config_id": "business-a",
            "config_version": 3,
            "tools": {
                "track_india_post": {
                    "enabled": True,
                    "permissions": {
                        "guest": False,
                        "registered_user": True,
                    },
                }
            },
        }
        trusted_context = {"user_id": 123}

        result = ask_openai(
            "Track AB123456789IN",
            client=client,
            model="mock-model",
            user_context=trusted_context,
            assistant_config=config,
        )

        self.assertEqual(result, "Tracking completed.")
        mock_execute_tool.assert_called_once_with(
            "track_india_post",
            function_call.arguments,
            user_context=trusted_context,
            assistant_config=config,
        )


    def test_authentication_failure_is_wrapped_without_tool_execution(self):
        error = AuthenticationError(
            message="mock authentication failure",
            response=Mock(),
            body=None,
        )
        client = MockOpenAIClient([])
        client.responses.create = Mock(side_effect=error)

        with patch("ai_tools.openai_agent.execute_tool") as mock_execute_tool:
            with self.assertRaises(OpenAIAgentError) as caught:
                ask_openai("Hello", client=client, model="mock-model")

        self.assertIs(caught.exception.__cause__, error)
        mock_execute_tool.assert_not_called()

    def test_rate_limit_failure_is_wrapped(self):
        error = RateLimitError(
            message="mock rate limit",
            response=Mock(),
            body=None,
        )
        client = MockOpenAIClient([])
        client.responses.create = Mock(side_effect=error)

        with self.assertRaises(OpenAIAgentError) as caught:
            ask_openai("Hello", client=client, model="mock-model")

        self.assertIs(caught.exception.__cause__, error)

    def test_connection_failure_is_wrapped(self):
        error = APIConnectionError(request=Mock())
        client = MockOpenAIClient([])
        client.responses.create = Mock(side_effect=error)

        with self.assertRaises(OpenAIAgentError) as caught:
            ask_openai("Hello", client=client, model="mock-model")

        self.assertIs(caught.exception.__cause__, error)

    def test_timeout_failure_is_wrapped(self):
        error = APITimeoutError(request=Mock())
        client = MockOpenAIClient([])
        client.responses.create = Mock(side_effect=error)

        with self.assertRaises(OpenAIAgentError) as caught:
            ask_openai("Hello", client=client, model="mock-model")

        self.assertIs(caught.exception.__cause__, error)

    def test_malformed_response_is_wrapped_safely(self):
        client = MockOpenAIClient([
            SimpleNamespace(id="response_malformed", output=None)
        ])

        with self.assertRaises(OpenAIAgentError) as caught:
            ask_openai("Hello", client=client, model="mock-model")

        self.assertIsNotNone(caught.exception.__cause__)



if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestDeterministicConsignmentToolGate(unittest.TestCase):

    @staticmethod
    def _run(current_message, requested_number, history=None):
        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({"consignment_number": requested_number}),
            call_id="gate-test-call",
        )
        client = MockOpenAIClient([
            make_response("gate-tool-response", [function_call]),
            make_response("gate-final-response", [], "Processed."),
        ])
        from ai_tools import openai_agent
        with patch.object(openai_agent, "resolve_instructions", return_value=""):
            with patch.object(openai_agent, "resolve_tool_schemas", return_value=[]):
                with patch.object(
                    openai_agent,
                    "resolve_config_identity",
                    return_value={"config_id": "test", "config_version": "1", "fingerprint": "test"},
                ):
                    with patch.object(openai_agent, "execute_tool", return_value={"success": True}) as executor:
                        result = ask_openai(
                            current_message,
                            client=client,
                            model="mock-model",
                            conversation_context_messages=history or [],
                        )
        return result, executor, client

    def test_single_valid_current_candidate_reaches_executor(self):
        _, executor, _ = self._run("Track AB123456789IN", "AB123456789IN")
        executor.assert_called_once()

    def test_multiple_current_candidates_are_blocked(self):
        result, executor, client = self._run(
            "Track AB123456789IN and CD987654321IN", "AB123456789IN"
        )
        executor.assert_not_called()
        self.assertEqual(len(client.responses.calls), 1)
        self.assertIn("more than one possible consignment number", result.lower())

    def test_invalid_current_candidate_is_blocked(self):
        _, executor, _ = self._run("Track AB12345", "AB12345")
        executor.assert_not_called()

    def test_model_substituted_number_is_blocked(self):
        _, executor, _ = self._run("Track AB123456789IN", "CD987654321IN")
        executor.assert_not_called()

    def test_single_historical_candidate_can_be_reused(self):
        _, executor, _ = self._run(
            "Where is my parcel?", "AB123456789IN",
            [{"role": "user", "content": "Track AB123456789IN"}],
        )
        executor.assert_called_once()

    def test_multiple_historical_candidates_are_blocked(self):
        _, executor, _ = self._run(
            "Where is that parcel?", "AB123456789IN",
            [
                {"role": "user", "content": "Track AB123456789IN"},
                {"role": "user", "content": "Also track CD987654321IN"},
            ],
        )
        executor.assert_not_called()

    def test_assistant_history_candidate_is_not_authorized(self):
        _, executor, _ = self._run(
            "Where is my parcel?", "AB123456789IN",
            [{"role": "assistant", "content": "AB123456789IN"}],
        )
        executor.assert_not_called()


class TestCorrectiveGuardrails(unittest.TestCase):
    def test_ambiguous_current_candidates_withhold_tracking_tool(self):
        from ai_tools.openai_agent import _tools_for_consignment_context

        tools = [
            {"type": "function", "name": "track_india_post"},
            {"type": "function", "name": "other_tool"},
        ]
        context = {
            "current_candidates": ["AB123456789IN", "CD987654321IN"],
            "current_invalid": [],
            "historical_candidates": [],
        }
        self.assertEqual(
            _tools_for_consignment_context(tools, context),
            [{"type": "function", "name": "other_tool"}],
        )

    def test_invalid_current_candidate_withholds_tracking_tool(self):
        from ai_tools.openai_agent import _tools_for_consignment_context

        tools = [{"type": "function", "name": "track_india_post"}]
        context = {
            "current_candidates": ["AB12345"],
            "current_invalid": ["AB12345"],
            "historical_candidates": [],
        }
        self.assertEqual(_tools_for_consignment_context(tools, context), [])

    def test_unambiguous_current_candidate_keeps_tracking_tool(self):
        from ai_tools.openai_agent import _tools_for_consignment_context

        tools = [{"type": "function", "name": "track_india_post"}]
        context = {
            "current_candidates": ["AB123456789IN"],
            "current_invalid": [],
            "historical_candidates": [],
        }
        self.assertEqual(_tools_for_consignment_context(tools, context), tools)

    def test_freshness_instructions_include_authoritative_current_date(self):
        from datetime import date
        from ai_tools.openai_agent import _format_tracking_freshness_instructions

        instructions = _format_tracking_freshness_instructions(
            date(2026, 11, 15)
        )
        self.assertIn("Current India-local date: 2026-11-15", instructions)
        self.assertIn("checkpoint 13 days old", instructions)

    def test_ambiguous_current_candidates_are_not_offered_to_model(self):
        from ai_tools import openai_agent

        client = MockOpenAIClient([
            make_response("ambiguous-no-tool", [], "Which number should I track?")
        ])
        with patch.object(openai_agent, "resolve_instructions", return_value=""):
            ask_openai(
                "Track AB123456789IN and CD987654321IN",
                client=client,
                model="mock-model",
            )

        offered_names = [
            tool.get("name") for tool in client.responses.calls[0]["tools"]
            if tool.get("type") == "function"
        ]
        self.assertNotIn("track_india_post", offered_names)
