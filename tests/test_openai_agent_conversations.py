import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

from database.db_connection import get_db_connection
from database.schema import (
    create_users_table,
    create_conversations_table,
    create_conversation_messages_table,
    create_conversation_requests_table,
    create_conversation_tool_rounds_table,
    create_conversation_tool_calls_table,
)
from database.conversation_repository import (
    create_conversation,
    get_conversation_messages,
)

from database.conversation_request_repository import (
    create_request,
    get_request,
    fail_request,
    interrupt_request,
)

from database.conversation_tool_call_repository import (
    list_tool_executions,
)

from database.conversation_tool_round_repository import (
    list_tool_rounds,
)

from ai_tools.openai_agent import (
    ask_openai,
    OpenAIAgentError,
    _select_bounded_conversation_history,
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


class TestOpenAIAgentConversationPersistence(unittest.TestCase):

    def test_persisted_conversation_passes_config_to_python_executor(self):
        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({"consignment_number": "AB123456789IN"}),
            call_id="call_persisted_config",
        )
        client = MockOpenAIClient([
            make_response("resp_config_1", [function_call]),
            make_response("resp_config_2", [], "Shipment lookup completed."),
        ])
        config = {
            "config_id": "business-persisted",
            "config_version": 4,
            "tools": {
                "track_india_post": {
                    "enabled": True,
                    "permissions": {
                        "guest": True,
                        "registered_user": False,
                    },
                }
            },
        }

        with mock.patch(
            "ai_tools.openai_agent.execute_tool",
            return_value={"success": True},
        ) as execute:
            result = ask_openai(
                "Track AB123456789IN",
                client=client,
                model="mock-model",
                conversation_id=self.conversation_id,
                assistant_config=config,
            )

        self.assertEqual(result, "Shipment lookup completed.")
        execute.assert_called_once_with(
            "track_india_post",
            function_call.arguments,
            assistant_config=config,
        )

    def test_resolved_instructions_are_sent_for_persisted_conversation(self):
        client = MockOpenAIClient([
            make_response("resp_with_config", [], "Hello from the configured assistant.")
        ])
        result = ask_openai(
            "Hello",
            client=client,
            model="mock-model",
            conversation_id=self.conversation_id,
            assistant_config={
                "config_id": "business_demo",
                "config_version": 1,
                "status": "active",
                "instructions": {
                    "business": ["Use the demonstration business identity."],
                },
            },
        )

        self.assertEqual(result, "Hello from the configured assistant.")
        self.assertIn(
            "Use the demonstration business identity.",
            client.responses.calls[0]["instructions"],
        )

    def test_shipment_response_policies_are_sent_for_persisted_conversation(self):
        client = MockOpenAIClient([
            make_response(
                "resp_persisted_policy",
                [],
                "The tracking record reports delivery.",
            )
        ])

        config = {
            "config_id": "business_policy_test",
            "config_version": 1,
            "status": "active",
            "business_rules": {
                "shipment_response_policies": {
                    "delivered": {
                        "additional_requirements": [
                            "Do not imply that the recipient personally received the item.",
                        ],
                    },
                },
            },
        }

        result = ask_openai(
            "What does the delivery status mean?",
            client=client,
            model="mock-model",
            conversation_id=self.conversation_id,
            assistant_config=config,
        )

        self.assertEqual(
            result,
            "The tracking record reports delivery.",
        )

        instructions = client.responses.calls[0]["instructions"]

        self.assertIn("SHIPMENT RESPONSE POLICIES", instructions)
        self.assertIn("DELIVERED", instructions)
        self.assertIn(
            "Do not imply that the recipient personally received the item.",
            instructions,
        )
        self.assertIn("PLATFORM REQUIREMENTS", instructions)
        self.assertIn("APPLICATION INSTRUCTIONS", instructions)

    def test_disabled_tools_are_not_sent_for_persisted_conversation(self):
        client = MockOpenAIClient([
            make_response("resp_tools_disabled", [], "Tracking is not enabled here.")
        ])
        result = ask_openai(
            "Can you track a shipment?",
            client=client,
            model="mock-model",
            conversation_id=self.conversation_id,
            assistant_config={"tools": {"track_india_post": {"enabled": False}}},
        )
        self.assertEqual(result, "Tracking is not enabled here.")
        self.assertEqual(client.responses.calls[0]["tools"], [])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "test_conversations.db"
        self.previous_db_path = os.environ.get("INDIAPOST_DB_PATH")
        os.environ["INDIAPOST_DB_PATH"] = str(self.database_path)

        create_users_table()
        create_conversations_table()
        create_conversation_messages_table()
        create_conversation_requests_table()
        create_conversation_tool_rounds_table()
        create_conversation_tool_calls_table()

        self.conversation_id = create_conversation(channel="test")

    def tearDown(self):
        if self.previous_db_path is None:
            os.environ.pop("INDIAPOST_DB_PATH", None)
        else:
            os.environ["INDIAPOST_DB_PATH"] = self.previous_db_path
        self.temp_dir.cleanup()

    def test_first_turn_is_persisted(self):
        client = MockOpenAIClient([
            make_response("resp_1", [], "Hello. How can I help?")
        ])

        result = ask_openai(
            "Hello",
            client=client,
            model="mock-model",
            conversation_id=self.conversation_id
        )

        self.assertEqual(result, "Hello. How can I help?")
        self.assertEqual(
            client.responses.calls[0]["input"],
            [{"role": "user", "content": "Hello"}]
        )

        messages = get_conversation_messages(self.conversation_id)
        self.assertEqual([item["role"] for item in messages], ["user", "assistant"])
        self.assertEqual(messages[0]["content"], "Hello")
        self.assertEqual(messages[1]["content"], "Hello. How can I help?")

    def test_follow_up_sends_bounded_persisted_history(self):
        first_client = MockOpenAIClient([
            make_response("resp_1", [], "Your shipment is in transit.")
        ])
        ask_openai(
            "What is the status?",
            client=first_client,
            model="mock-model",
            conversation_id=self.conversation_id
        )

        second_client = MockOpenAIClient([
            make_response("resp_2", [], "It has not been delivered yet.")
        ])
        with mock.patch.dict(os.environ, {"OPENAI_MAX_HISTORY_CHARS": "25"}):
            result = ask_openai(
                "Has it been delivered?",
                client=second_client,
                model="mock-model",
                conversation_id=self.conversation_id
            )

        self.assertEqual(result, "It has not been delivered yet.")
        self.assertEqual(
            second_client.responses.calls[0]["input"],
            [{"role": "user", "content": "Has it been delivered?"}]
        )

        messages = get_conversation_messages(self.conversation_id)
        self.assertEqual(len(messages), 4)

    def test_function_call_and_output_are_persisted(self):
        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({"consignment_number": "AB123456789IN"}),
            call_id="call_123"
        )
        client = MockOpenAIClient([
            make_response("resp_1", [function_call]),
            make_response("resp_2", [], "The shipment is in transit.")
        ])

        from unittest.mock import patch
        with patch(
            "ai_tools.openai_agent.execute_tool",
            return_value={"success": True, "status": "In Transit"}
        ):
            result = ask_openai(
                "Track AB123456789IN",
                client=client,
                model="mock-model",
                conversation_id=self.conversation_id
            )

        self.assertEqual(result, "The shipment is in transit.")

        messages = get_conversation_messages(self.conversation_id)
        self.assertEqual(
            [item["role"] for item in messages],
            ["user", "assistant", "tool", "assistant"]
        )
        self.assertEqual(messages[1]["tool_name"], "track_india_post")
        self.assertEqual(messages[1]["tool_call_id"], "call_123")
        self.assertEqual(messages[2]["tool_call_id"], "call_123")
        
        executions = list_tool_executions(self.conversation_id)

        self.assertEqual(len(executions), 1)

        execution = executions[0]

        self.assertEqual(execution["tool_call_id"], "call_123")
        self.assertEqual(execution["tool_name"], "track_india_post")
        self.assertEqual(execution["status"], "completed")
        self.assertEqual(execution["round_number"], 1)
        self.assertEqual(
            execution["call_message_id"],
            messages[1]["message_id"]
        )
        self.assertEqual(
            execution["output_message_id"],
            messages[2]["message_id"]
        )

        rounds = list_tool_rounds(self.conversation_id)
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["round_number"], 1)
        self.assertEqual(rounds[0]["status"], "completed")
        self.assertEqual(rounds[0]["expected_call_count"], 1)
        self.assertEqual(execution["round_id"], rounds[0]["round_id"])
        
        second_input = client.responses.calls[1]["input"]
        self.assertEqual(second_input[0]["type"], "function_call_output")
        self.assertEqual(second_input[0]["call_id"], "call_123")

    def test_history_selector_prefers_newest_complete_messages(self):
        messages = [
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "new question"},
            {"role": "assistant", "content": "new answer"},
            {"role": "user", "content": "current question"},
        ]

        selected = _select_bounded_conversation_history(messages, 40)

        self.assertEqual(
            [item["content"] for item in selected],
            ["new question", "new answer", "current question"],
        )

    def test_history_selector_keeps_complete_multi_call_tool_round(self):
        messages = [
            {"role": "user", "content": "older"},
            {"role": "assistant", "content": "call one", "tool_name": "track", "tool_call_id": "c1"},
            {"role": "assistant", "content": "call two", "tool_name": "track", "tool_call_id": "c2"},
            {"role": "tool", "content": "result one", "tool_call_id": "c1"},
            {"role": "tool", "content": "result two", "tool_call_id": "c2"},
            {"role": "assistant", "content": "summary"},
            {"role": "user", "content": "current"},
        ]

        selected = _select_bounded_conversation_history(messages, 100)

        self.assertEqual(selected, messages)
        selected_without_round_budget = _select_bounded_conversation_history(
            messages, 13
        )
        self.assertEqual(
            [item["content"] for item in selected_without_round_budget],
            ["current"],
        )

    def test_history_selector_retains_oversized_current_user_message(self):
        messages = [
            {"role": "user", "content": "older"},
            {"role": "assistant", "content": "older answer"},
            {"role": "user", "content": "x" * 100},
        ]

        selected = _select_bounded_conversation_history(messages, 10)

        self.assertEqual(selected, [messages[-1]])
        self.assertEqual(len(selected[0]["content"]), 100)

    def test_invalid_history_budget_is_rejected_before_api_call(self):
        client = MockOpenAIClient([
            make_response("resp_1", [], "This should not be returned.")
        ])

        with mock.patch.dict(
            os.environ, {"OPENAI_MAX_HISTORY_CHARS": "not-a-number"}
        ):
            with self.assertRaises(OpenAIAgentError):
                ask_openai(
                    "Hello",
                    client=client,
                    model="mock-model",
                    conversation_id=self.conversation_id,
                )

        self.assertEqual(client.responses.calls, [])

    def test_unknown_conversation_is_rejected_before_api_call(self):
        client = MockOpenAIClient([])

        with self.assertRaises(OpenAIAgentError):
            ask_openai(
                "Hello",
                client=client,
                model="mock-model",
                conversation_id="not-a-valid-conversation"
            )

        self.assertEqual(client.responses.calls, [])

    def test_closed_conversation_is_rejected_before_api_call(self):
        from database.conversation_repository import close_conversation

        self.assertTrue(close_conversation(self.conversation_id))
        client = MockOpenAIClient([])

        with self.assertRaises(OpenAIAgentError):
            ask_openai(
                "Hello",
                client=client,
                model="mock-model",
                conversation_id=self.conversation_id
            )

        self.assertEqual(client.responses.calls, [])

    def test_registered_conversation_requires_matching_user_context(self):
        connection = get_db_connection()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO users (external_user_id, created_at) VALUES (?, ?)",
                ("test-user", "2026-01-01T00:00:00")
            )
            connection.commit()
            user_id = cursor.lastrowid
        finally:
            connection.close()

        registered_conversation = create_conversation(
            channel="test",
            user_id=user_id
        )
        client = MockOpenAIClient([])

        with self.assertRaises(OpenAIAgentError):
            ask_openai(
                "Hello",
                client=client,
                model="mock-model",
                conversation_id=registered_conversation
            )

        self.assertEqual(client.responses.calls, [])

    def test_unexpected_tool_exception_is_persisted_as_error_output(self):
        from unittest.mock import patch

        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({
                "consignment_number": "AB123456789IN"
            }),
            call_id="call_error_123"
        )

        client = MockOpenAIClient([
            make_response("resp_error_1", [function_call]),
            make_response(
                "resp_error_2",
                [],
                "I could not retrieve the shipment information."
            )
        ])

        with patch(
            "ai_tools.openai_agent.execute_tool",
            side_effect=RuntimeError("Simulated unexpected tool failure")
        ):
            result = ask_openai(
                "Track AB123456789IN",
                client=client,
                model="mock-model",
                conversation_id=self.conversation_id
            )

        self.assertEqual(
            result,
            "I could not retrieve the shipment information."
        )

        messages = get_conversation_messages(self.conversation_id)

        self.assertEqual(
            [message["role"] for message in messages],
            ["user", "assistant", "tool", "assistant"]
        )

        self.assertEqual(
            messages[1]["tool_call_id"],
            "call_error_123"
        )

        self.assertEqual(
            messages[2]["tool_call_id"],
            "call_error_123"
        )

        tool_result = json.loads(messages[2]["content"])

        self.assertEqual(tool_result["success"], False)
        self.assertEqual(
            tool_result["error"],
            "Tool execution failed."
        )

        self.assertEqual(
            client.responses.calls[1]["input"][0]["type"],
            "function_call_output"
        )


    def test_multiple_function_calls_are_persisted_and_reconstructed(self):
        from unittest.mock import patch

        function_call_1 = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({
                "consignment_number": "AB123456789IN"
            }),
            call_id="call_multi_1"
        )

        function_call_2 = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({
                "consignment_number": "CD987654321IN"
            }),
            call_id="call_multi_2"
        )

        first_client = MockOpenAIClient([
            make_response(
                "resp_multi_1",
                [function_call_1, function_call_2]
            ),
            make_response(
                "resp_multi_2",
                [],
                "Both shipment checks are complete."
            )
        ])

        with patch(
            "ai_tools.openai_agent.execute_tool",
            side_effect=[
                {"success": True, "status": "In Transit"},
                {"success": True, "status": "Delivered"}
            ]
        ) as mock_execute:
            result = ask_openai(
                "Track AB123456789IN and CD987654321IN.",
                client=first_client,
                model="mock-model",
                conversation_id=self.conversation_id
            )

        self.assertEqual(
            result,
            "I found more than one possible consignment number in your "
            "message. Which one would you like me to track?"
        )
        self.assertEqual(mock_execute.call_count, 0)
        self.assertEqual(len(first_client.responses.calls), 1)

        messages = get_conversation_messages(self.conversation_id)

        # Both assistant function calls should be stored before
        # their corresponding tool outputs.
        self.assertEqual(
            [message["role"] for message in messages],
            ["user", "assistant", "assistant", "tool", "tool", "assistant"]
        )

        self.assertEqual(
            [message["tool_call_id"] for message in messages[1:5]],
            [
                "call_multi_1",
                "call_multi_2",
                "call_multi_1",
                "call_multi_2"
            ]
        )

        executions = list_tool_executions(self.conversation_id)

        self.assertEqual(len(executions), 2)

        executions_by_call_id = {
            item["tool_call_id"]: item
            for item in executions
        }

        self.assertEqual(
            set(executions_by_call_id.keys()),
            {"call_multi_1", "call_multi_2"}
        )

        first_execution = executions_by_call_id["call_multi_1"]
        second_execution = executions_by_call_id["call_multi_2"]

        self.assertEqual(first_execution["status"], "completed")
        self.assertEqual(second_execution["status"], "completed")

        self.assertEqual(first_execution["round_number"], 1)
        self.assertEqual(second_execution["round_number"], 1)

        rounds = list_tool_rounds(self.conversation_id)
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["round_number"], 1)
        self.assertEqual(rounds[0]["status"], "completed")
        self.assertEqual(rounds[0]["expected_call_count"], 2)
        self.assertEqual(first_execution["round_id"], rounds[0]["round_id"])
        self.assertEqual(second_execution["round_id"], rounds[0]["round_id"])

        self.assertEqual(
            first_execution["call_message_id"],
            messages[1]["message_id"]
        )
        self.assertEqual(
            second_execution["call_message_id"],
            messages[2]["message_id"]
        )

        self.assertEqual(
            first_execution["output_message_id"],
            messages[3]["message_id"]
        )
        self.assertEqual(
            second_execution["output_message_id"],
            messages[4]["message_id"]
        )

        # Rejected ambiguous calls are persisted for audit, but no second
        # model request is made after Python supplies the clarification.
        self.assertEqual(len(first_client.responses.calls), 1)

        # Start another turn and verify the persisted history
        # is reconstructed for the next Responses API request.
        second_client = MockOpenAIClient([
            make_response(
                "resp_multi_3",
                [],
                "Both shipment results are available."
            )
        ])

        follow_up_result = ask_openai(
            "What were the results?",
            client=second_client,
            model="mock-model",
            conversation_id=self.conversation_id
        )

        self.assertEqual(
            follow_up_result,
            "Both shipment results are available."
        )

        reconstructed_input = second_client.responses.calls[0]["input"]

        self.assertEqual(
            [item.get("role") for item in reconstructed_input],
            [
                "user",
                None,
                None,
                None,
                None,
                "assistant",
                "user"
            ]
        )

        self.assertEqual(
            [item.get("call_id") for item in reconstructed_input[1:5]],
            [
                "call_multi_1",
                "call_multi_2",
                "call_multi_1",
                "call_multi_2"
            ]
        )

        self.assertEqual(
            reconstructed_input[1]["type"],
            "function_call"
        )
        self.assertEqual(
            reconstructed_input[2]["type"],
            "function_call"
        )
        self.assertEqual(
            reconstructed_input[3]["type"],
            "function_call_output"
        )
        self.assertEqual(
            reconstructed_input[4]["type"],
            "function_call_output"
        )

        self.assertEqual(
            reconstructed_input[-1]["content"],
            "What were the results?"
        )

    def test_tool_output_persistence_failure_interrupts_execution_and_request(self):
        from unittest.mock import patch

        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({
                "consignment_number": "AB123456789IN"
            }),
            call_id="call_interrupted_123"
        )

        client = MockOpenAIClient([
            make_response("resp_interrupted_1", [function_call])
        ])

        with patch(
            "ai_tools.openai_agent.execute_tool",
            return_value={"success": True, "status": "In Transit"}
        ) as mock_execute:
            with patch(
                "ai_tools.openai_agent._complete_tool_execution",
                side_effect=RuntimeError("Simulated output persistence failure")
            ):
                with self.assertRaises(OpenAIAgentError):
                    ask_openai(
                        "Track AB123456789IN",
                        client=client,
                        model="mock-model",
                        conversation_id=self.conversation_id,
                        request_id="request-interrupted-001"
                    )

        # The tool ran, but its output could not be persisted.
        mock_execute.assert_called_once()

        # The agent must not make a follow-up OpenAI request.
        self.assertEqual(len(client.responses.calls), 1)

        # The request must be marked as interrupted.
        request = get_request("request-interrupted-001")

        self.assertIsNotNone(request)
        self.assertEqual(request["status"], "interrupted")

        # The tool execution must also be marked as interrupted.
        executions = list_tool_executions(self.conversation_id)

        self.assertEqual(len(executions), 1)
        self.assertEqual(
            executions[0]["tool_call_id"],
            "call_interrupted_123"
        )
        self.assertEqual(executions[0]["status"], "interrupted")

        rounds = list_tool_rounds(
            self.conversation_id,
            request_id="request-interrupted-001",
        )
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["status"], "interrupted")
        self.assertEqual(rounds[0]["expected_call_count"], 1)
        self.assertEqual(executions[0]["round_id"], rounds[0]["round_id"])


    def test_request_id_creates_and_completes_request(self):
        client = MockOpenAIClient([
            make_response("resp_idem_1", [], "Your shipment is in transit.")
        ])

        result = ask_openai(
            "Track my shipment",
            client=client,
            model="mock-model",
            conversation_id=self.conversation_id,
            request_id="request-001",
        )

        self.assertEqual(result, "Your shipment is in transit.")

        request = get_request("request-001")

        self.assertIsNotNone(request)
        self.assertEqual(request["status"], "completed")
        self.assertIsNotNone(request["user_message_id"])
        self.assertIsNotNone(request["response_message_id"])

        messages = get_conversation_messages(self.conversation_id)

        self.assertEqual(
            [message["role"] for message in messages],
            ["user", "assistant"],
        )

    def test_completed_duplicate_returns_saved_response_without_api_call(self):
        first_client = MockOpenAIClient([
            make_response("resp_idem_2", [], "The shipment has been delivered.")
        ])

        first_result = ask_openai(
            "Track my shipment",
            client=first_client,
            model="mock-model",
            conversation_id=self.conversation_id,
            request_id="request-002",
        )

        second_client = MockOpenAIClient([])

        second_result = ask_openai(
            "Track my shipment",
            client=second_client,
            model="mock-model",
            conversation_id=self.conversation_id,
            request_id="request-002",
        )

        self.assertEqual(first_result, second_result)
        self.assertEqual(
            second_result,
            "The shipment has been delivered.",
        )
        self.assertEqual(second_client.responses.calls, [])

        messages = get_conversation_messages(self.conversation_id)

        self.assertEqual(len(messages), 2)

    def test_processing_duplicate_is_not_executed_again(self):
        create_request(
            request_id="request-003",
            conversation_id=self.conversation_id,
            user_message="Track my shipment",
        )

        client = MockOpenAIClient([])

        with self.assertRaises(OpenAIAgentError):
            ask_openai(
                "Track my shipment",
                client=client,
                model="mock-model",
                conversation_id=self.conversation_id,
                request_id="request-003",
            )

        self.assertEqual(client.responses.calls, [])

        request = get_request("request-003")
        self.assertEqual(request["status"], "processing")

    def test_failed_duplicate_is_not_executed_again(self):
        create_request(
            request_id="request-004",
            conversation_id=self.conversation_id,
            user_message="Track my shipment",
        )

        self.assertTrue(
            fail_request(
                request_id="request-004",
                error_code="test_failure",
            )
        )

        client = MockOpenAIClient([])

        with self.assertRaises(OpenAIAgentError):
            ask_openai(
                "Track my shipment",
                client=client,
                model="mock-model",
                conversation_id=self.conversation_id,
                request_id="request-004",
            )

        self.assertEqual(client.responses.calls, [])

        request = get_request("request-004")
        self.assertEqual(request["status"], "failed")

    def test_interrupted_duplicate_is_not_executed_again(self):
        create_request(
            request_id="request-005",
            conversation_id=self.conversation_id,
            user_message="Track my shipment",
        )

        self.assertTrue(
            interrupt_request(
                request_id="request-005",
                error_code="test_interruption",
            )
        )

        client = MockOpenAIClient([])

        with self.assertRaises(OpenAIAgentError):
            ask_openai(
                "Track my shipment",
                client=client,
                model="mock-model",
                conversation_id=self.conversation_id,
                request_id="request-005",
            )

        self.assertEqual(client.responses.calls, [])

        request = get_request("request-005")
        self.assertEqual(request["status"], "interrupted")

    def test_request_id_cannot_be_reused_with_different_message(self):
        create_request(
            request_id="request-006",
            conversation_id=self.conversation_id,
            user_message="Track my shipment",
        )

        client = MockOpenAIClient([])

        with self.assertRaises(OpenAIAgentError):
            ask_openai(
                "Cancel my shipment",
                client=client,
                model="mock-model",
                conversation_id=self.conversation_id,
                request_id="request-006",
            )

        self.assertEqual(client.responses.calls, [])

        messages = get_conversation_messages(self.conversation_id)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["content"], "Track my shipment")

    def test_openai_failure_marks_new_request_as_failed(self):
        client = MockOpenAIClient([])

        with self.assertRaises(OpenAIAgentError):
            ask_openai(
                "Track my shipment",
                client=client,
                model="mock-model",
                conversation_id=self.conversation_id,
                request_id="request-007",
            )

        request = get_request("request-007")

        self.assertIsNotNone(request)
        self.assertEqual(request["status"], "failed")
        self.assertEqual(
            request["error_code"],
            "unexpected_error",
        )

if __name__ == "__main__":
    unittest.main(verbosity=2)
