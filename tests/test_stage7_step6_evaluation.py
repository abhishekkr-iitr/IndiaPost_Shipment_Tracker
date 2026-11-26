import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from ai_tools import tool_executor

from stage7_step6_evaluation import (
    build_real_model_recording_client,
    RecordingOpenAIClient,
    RecordingResponses,
    APIRequestBudget,
    APIRequestBudgetExceeded,
    create_evaluation_record,
    execute_tool_with_recording,
    isolated_fixture_tool_executor,
    load_scenario_specifications,
    serialize_evaluation_records,
    validate_evaluation_records,
    validate_real_model_evaluation_config,
    create_fixture_tool_executor,
    create_blocking_tool_executor,
    run_offline_evaluation,
    sanitize_evaluation_data,
    run_real_model_scenario,
    resolve_scenario_fixture,
    run_real_model_evaluation,
)


VALID_CONSIGNMENT = "AB123456789IN"

class TestStage7Step6RealModelClientBuilder(unittest.TestCase):

    def test_builder_requires_model_identifier(self):
        with self.assertRaises(ValueError):
            build_real_model_recording_client("")

    def test_builder_rejects_invalid_timeout(self):
        invalid_values = [
            0,
            -1,
            True,
            float("inf"),
            float("nan"),
            "30",
        ]

        for value in invalid_values:
            with self.subTest(timeout_seconds=value):
                with self.assertRaises(ValueError):
                    build_real_model_recording_client(
                        "test-model",
                        timeout_seconds=value,
                    )

    def test_builder_requires_api_key(self):
        with patch.dict(
            "os.environ",
            {"OPENAI_API_KEY": ""},
        ):
            with self.assertRaises(RuntimeError):
                build_real_model_recording_client("test-model")

    def test_builder_configures_client_without_making_api_call(self):
        fake_responses = type(
            "FakeResponses",
            (),
            {"create": lambda self, **kwargs: None},
        )()

        fake_client = type(
            "FakeClient",
            (),
            {"responses": fake_responses},
        )()

        with patch.dict(
            "os.environ",
            {"OPENAI_API_KEY": "test-only-not-a-real-key"},
        ):
            with patch(
                "openai.OpenAI",
                return_value=fake_client,
            ) as openai_constructor:
                recording_client = build_real_model_recording_client(
                    "test-model",
                    timeout_seconds=45,
                )

        self.assertIsInstance(
            recording_client,
            RecordingOpenAIClient,
        )

        openai_constructor.assert_called_once_with(
            api_key="test-only-not-a-real-key",
            timeout=45,
            max_retries=0,
        )

        self.assertEqual(
            recording_client.responses.calls,
            [],
        )

class TestStage7Step6ReportSanitization(unittest.TestCase):

    def test_sensitive_dictionary_fields_are_redacted_recursively(self):
        source = {
            "api_key": "sk-test-secret-value-123456",
            "authorization": "Bearer confidential-token",
            "user_id": 12345,
            "nested": {
                "password": "private-password",
                "headers": {
                    "Authorization": "Bearer another-token",
                },
            },
        }

        sanitized = sanitize_evaluation_data(source)

        self.assertEqual(
            sanitized["api_key"],
            "[REDACTED]",
        )
        self.assertEqual(
            sanitized["authorization"],
            "[REDACTED]",
        )
        self.assertEqual(
            sanitized["user_id"],
            "[REDACTED]",
        )
        self.assertEqual(
            sanitized["nested"]["password"],
            "[REDACTED]",
        )
        self.assertEqual(
            sanitized["nested"]["headers"],
            "[REDACTED]",
        )

    def test_sensitive_text_patterns_are_redacted(self):
        source = (
            "Key sk-testsecretvalue123456 "
            "Bearer confidential-token "
            "Contact person@example.com "
            "or +91 9876543210."
        )

        sanitized = sanitize_evaluation_data(source)

        self.assertNotIn("sk-testsecretvalue123456", sanitized)
        self.assertNotIn("confidential-token", sanitized)
        self.assertNotIn("person@example.com", sanitized)
        self.assertNotIn("9876543210", sanitized)

        self.assertIn("[REDACTED_API_KEY]", sanitized)
        self.assertIn("Bearer [REDACTED]", sanitized)
        self.assertIn("[REDACTED_EMAIL]", sanitized)
        self.assertIn("[REDACTED_PHONE]", sanitized)

    def test_sanitization_does_not_modify_original_data(self):
        source = {
            "api_key": "private-key",
            "message": "Contact person@example.com",
        }

        original = json.loads(json.dumps(source))

        sanitize_evaluation_data(source)

        self.assertEqual(source, original)

    def test_synthetic_consignment_number_is_preserved(self):
        source = {
            "consignment_number": "AB123456789IN",
        }

        sanitized = sanitize_evaluation_data(source)

        self.assertEqual(
            sanitized["consignment_number"],
            "AB123456789IN",
        )

    def test_serializer_applies_sanitization(self):
        record = create_evaluation_record(
            scenario_id="C02",
            user_input="Track AB123456789IN",
            model_identifier="test-model",
            expected_decision="track",
        )

        record["model_api_calls"] = [{
            "request": {
                "api_key": "sk-test-secret-value-123456",
                "input": "Contact person@example.com",
            },
            "response": {
                "output_text": "Bearer confidential-token",
            },
        }]

        serialized = serialize_evaluation_records([record])

        self.assertNotIn("sk-test-secret-value-123456", serialized)
        self.assertNotIn("person@example.com", serialized)
        self.assertNotIn("confidential-token", serialized)

        self.assertIn("[REDACTED]", serialized)
        self.assertIn("[REDACTED_EMAIL]", serialized)
        self.assertIn("AB123456789IN", serialized)


class TestOpenAIAgentExecutorInjection(unittest.TestCase):

    def test_ask_openai_uses_injected_executor(self):
        from ai_tools import openai_agent

        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments='{"consignment_number": "AB123456789IN"}',
            call_id="injected-call",
        )
        responses = [
            SimpleNamespace(id="mock_response_tool", output=[function_call], output_text=""),
            SimpleNamespace(id="mock_response_final", output=[], output_text="Fixture result"),
        ]

        class FakeResponses:
            def create(self, **kwargs):
                return responses.pop(0)

        class FakeClient:
            responses = FakeResponses()

        calls = []

        def injected_executor(name, arguments, **kwargs):
            calls.append((name, arguments, kwargs))
            return {"success": True, "data": {"current_status": "Delivered"}}

        with patch.object(openai_agent, "resolve_instructions", return_value=""):
            with patch.object(openai_agent, "resolve_tool_schemas", return_value=[]):
                with patch.object(
                    openai_agent,
                    "resolve_config_identity",
                    return_value={
                        "config_id": "test",
                        "config_version": "1",
                        "fingerprint": "test",
                    },
                ):
                    result = openai_agent.ask_openai(
                        "Track AB123456789IN",
                        client=FakeClient(),
                        model="mock-model",
                        tool_executor=injected_executor,
                    )

        self.assertEqual(result, "Fixture result")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "track_india_post")

    def test_ask_openai_rejects_non_callable_executor_before_api_call(self):
        from ai_tools import openai_agent

        class FakeResponses:
            called = False
            def create(self, **kwargs):
                self.called = True
                raise AssertionError("API must not be called.")

        fake_client = SimpleNamespace(responses=FakeResponses())
        with self.assertRaises(TypeError):
            openai_agent.ask_openai(
                "Hello",
                client=fake_client,
                model="mock-model",
                tool_executor=object(),
            )
        self.assertFalse(fake_client.responses.called)


class TestStage7Step6StrengthenedReportValidation(unittest.TestCase):

    def make_report(self):
        return [
            create_evaluation_record(
                item["id"],
                item["user_input"],
                "mock-model",
                item["expected_decision"],
            )
            for item in load_scenario_specifications()
        ]

    def test_invalid_model_decision_is_rejected(self):
        records = self.make_report()
        records[0]["actual_model_decision"] = "maybe"
        with self.assertRaises(ValueError):
            validate_evaluation_records(records)

    def test_invalid_score_range_is_rejected(self):
        records = self.make_report()
        records[0]["scores"].update({
            "intent_and_decision": 2,
            "tool_use_decision": 1,
            "evidence_use": 2,
            "uncertainty_and_qualification": 2,
            "prohibited_claim_avoidance": 2,
            "clarification_or_refusal": 1,
            "applicable_maximum": 9,
            "earned": 10,
        })
        with self.assertRaises(ValueError):
            validate_evaluation_records(records)

    def test_score_totals_must_match_components(self):
        records = self.make_report()
        records[0]["scores"].update({
            "intent_and_decision": 1,
            "tool_use_decision": 1,
            "evidence_use": 2,
            "uncertainty_and_qualification": 2,
            "prohibited_claim_avoidance": 2,
            "clarification_or_refusal": 1,
            "applicable_maximum": 9,
            "earned": 8,
        })
        with self.assertRaises(ValueError):
            validate_evaluation_records(records)

    def test_tool_call_schema_is_validated(self):
        records = self.make_report()
        records[0]["tool_calls"] = [{"tool_name": "track_india_post"}]
        with self.assertRaises(ValueError):
            validate_evaluation_records(records)


class TestStage7Step6ExecutorDependencyInjection(unittest.TestCase):

    def test_per_call_registry_does_not_mutate_production_registry(self):
        original = tool_executor.AVAILABLE_TOOLS["track_india_post"]
        calls = []

        def fixture(number):
            calls.append(number)
            return {"success": True, "data": {"current_status": "Fixture"}}

        result = tool_executor.execute_tool(
            "track_india_post",
            {"consignment_number": VALID_CONSIGNMENT},
            tool_registry={"track_india_post": fixture},
        )

        self.assertTrue(result["success"])
        self.assertEqual(calls, [VALID_CONSIGNMENT])
        self.assertIs(tool_executor.AVAILABLE_TOOLS["track_india_post"], original)

    def test_registry_cannot_introduce_unapproved_tool(self):
        result = tool_executor.execute_tool(
            "unexpected_tool",
            {"consignment_number": VALID_CONSIGNMENT},
            tool_registry={"unexpected_tool": lambda value: {"success": True}},
        )
        self.assertFalse(result["success"])
        self.assertIn("Unknown or unauthorized tool", result["error"])

    def test_invalid_registry_is_rejected(self):
        result = tool_executor.execute_tool(
            "track_india_post",
            {"consignment_number": VALID_CONSIGNMENT},
            tool_registry=[],
        )
        self.assertFalse(result["success"])

    def test_production_registry_remains_default(self):
        original = tool_executor.AVAILABLE_TOOLS["track_india_post"]
        self.assertTrue(callable(original))
        self.assertIs(tool_executor.AVAILABLE_TOOLS["track_india_post"], original)


class TestStage7Step6APIRequestBudget(unittest.TestCase):

    def test_budget_counts_successful_requests(self):
        class Responses:
            def create(self, **kwargs):
                return {"id": "ok"}

        budget = APIRequestBudget(2)
        wrapped = RecordingResponses(Responses(), request_budget=budget)
        wrapped.create(model="mock")
        wrapped.create(model="mock")
        self.assertEqual(budget.requests_used, 2)
        self.assertTrue(budget.exhausted)

    def test_budget_counts_failed_requests(self):
        class Responses:
            def create(self, **kwargs):
                raise RuntimeError("simulated failure")

        budget = APIRequestBudget(1)
        wrapped = RecordingResponses(Responses(), request_budget=budget)
        with self.assertRaises(RuntimeError):
            wrapped.create(model="mock")
        self.assertEqual(budget.requests_used, 1)
        self.assertTrue(wrapped.calls[-1]["forwarded"])
        self.assertTrue(wrapped.calls[-1]["failed"])
        self.assertFalse(wrapped.calls[-1]["budget_blocked"])

    def test_exhausted_budget_blocks_before_forwarding(self):
        class Responses:
            calls = 0
            def create(self, **kwargs):
                self.calls += 1
                return {"id": "ok"}

        original = Responses()
        budget = APIRequestBudget(1)
        wrapped = RecordingResponses(original, request_budget=budget)
        wrapped.create(model="mock")
        with self.assertRaises(APIRequestBudgetExceeded):
            wrapped.create(model="mock")
        self.assertEqual(original.calls, 1)
        self.assertTrue(wrapped.calls[-1]["budget_blocked"])
        self.assertFalse(wrapped.calls[-1]["forwarded"])
        self.assertFalse(wrapped.calls[-1]["failed"])

    def test_invalid_budget_values_are_rejected(self):
        for value in (0, -1, True, 1.5, 1001, "5"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    APIRequestBudget(value)


class TestStage7Step6ExecutorFixtureIntegration(unittest.TestCase):

    def test_authorized_guest_tracking_uses_synthetic_fixture(self):
        original_function = tool_executor.AVAILABLE_TOOLS[
            "track_india_post"
        ]

        with isolated_fixture_tool_executor("C02") as provider:
            result = tool_executor.execute_tool(
                "track_india_post",
                {"consignment_number": VALID_CONSIGNMENT},
                tool_registry={"track_india_post": provider.track_india_post},
            )

            self.assertTrue(result["success"])
            self.assertEqual(
                result["data"]["current_status"],
                "Delivered",
            )

            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(
                provider.calls[0]["consignment_number"],
                VALID_CONSIGNMENT,
            )
            self.assertIsNone(provider.calls[0]["user_id"])

            self.assertIs(
                tool_executor.AVAILABLE_TOOLS["track_india_post"],
                original_function,
            )

        self.assertIs(
            tool_executor.AVAILABLE_TOOLS["track_india_post"],
            original_function,
        )

    def test_authorized_registered_user_forwards_trusted_user_id(self):
        with isolated_fixture_tool_executor("C02") as provider:
            result = tool_executor.execute_tool(
                "track_india_post",
                {"consignment_number": VALID_CONSIGNMENT},
                user_context={"user_id": 123},
                tool_registry={"track_india_post": provider.track_india_post},
            )

            self.assertTrue(result["success"])
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(
                provider.calls[0]["user_id"],
                123,
            )

    def test_model_supplied_user_id_is_rejected_before_fixture_call(self):
        with isolated_fixture_tool_executor("C02") as provider:
            result = tool_executor.execute_tool(
                "track_india_post",
                {
                    "consignment_number": VALID_CONSIGNMENT,
                    "user_id": 123,
                },
                tool_registry={"track_india_post": provider.track_india_post},
            )

            self.assertFalse(result["success"])
            self.assertEqual(
                result["error"],
                "Invalid tool arguments.",
            )
            self.assertEqual(provider.calls, [])

    def test_unknown_tool_is_rejected_before_fixture_call(self):
        with isolated_fixture_tool_executor("C02") as provider:
            result = tool_executor.execute_tool(
                "unknown_tool",
                {"consignment_number": VALID_CONSIGNMENT},
                tool_registry={"track_india_post": provider.track_india_post},
            )

            self.assertFalse(result["success"])
            self.assertIn(
                "Unknown or unauthorized tool",
                result["error"],
            )
            self.assertEqual(provider.calls, [])

    def test_guest_permission_denial_prevents_fixture_call(self):
        restricted_config = {
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

        with isolated_fixture_tool_executor("C02") as provider:
            result = tool_executor.execute_tool(
                "track_india_post",
                {"consignment_number": VALID_CONSIGNMENT},
                assistant_config=restricted_config,
                tool_registry={"track_india_post": provider.track_india_post},
            )

            self.assertFalse(result["success"])
            self.assertEqual(
                result["error"],
                "Tool is not permitted for this user.",
            )
            self.assertEqual(provider.calls, [])

    def test_registered_user_permission_denial_prevents_fixture_call(self):
        restricted_config = {
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

        with isolated_fixture_tool_executor("C02") as provider:
            result = tool_executor.execute_tool(
                "track_india_post",
                {"consignment_number": VALID_CONSIGNMENT},
                user_context={"user_id": 123},
                assistant_config=restricted_config,
                tool_registry={"track_india_post": provider.track_india_post},
            )

            self.assertFalse(result["success"])
            self.assertEqual(
                result["error"],
                "Tool is not permitted for this user.",
            )
            self.assertEqual(provider.calls, [])

    def test_global_registry_is_unchanged_after_fixture_context_exception(self):
        original_function = tool_executor.AVAILABLE_TOOLS["track_india_post"]

        with self.assertRaisesRegex(RuntimeError, "simulated integration failure"):
            with isolated_fixture_tool_executor("C02") as provider:
                self.assertIs(
                    tool_executor.AVAILABLE_TOOLS["track_india_post"],
                    original_function,
                )
                self.assertIsNot(provider.track_india_post, original_function)
                raise RuntimeError("simulated integration failure")

        self.assertIs(
            tool_executor.AVAILABLE_TOOLS["track_india_post"],
            original_function,
        )

    def test_scenario_without_fixture_is_rejected(self):
        original_function = tool_executor.AVAILABLE_TOOLS[
            "track_india_post"
        ]

        with self.assertRaisesRegex(
            ValueError,
            "does not have a tracking fixture",
        ):
            with isolated_fixture_tool_executor("A04"):
                self.fail("A04 should not enter the fixture context.")

        self.assertIs(
            tool_executor.AVAILABLE_TOOLS["track_india_post"],
            original_function,
        )


class TestStage7Step6PythonEnforcementRecording(unittest.TestCase):

    def make_record(self):
        return create_evaluation_record(
            scenario_id="C02",
            user_input="Track the shipment.",
            model_identifier="mock-model",
            expected_decision="track",
        )

    def test_authorized_guest_outcome_is_recorded(self):
        record = self.make_record()

        with isolated_fixture_tool_executor("C02") as provider:
            result = execute_tool_with_recording(
                record,
                "track_india_post",
                {"consignment_number": VALID_CONSIGNMENT},
                provider,
            )

        enforcement = record["python_enforcement"]

        self.assertTrue(result["success"])
        self.assertEqual(
            enforcement["outcome"],
            "allowed_and_executed",
        )
        self.assertEqual(enforcement["caller_role"], "guest")
        self.assertTrue(enforcement["registered_tool"])
        self.assertTrue(enforcement["function_invoked"])
        self.assertEqual(enforcement["fixture_call_count"], 1)
        self.assertTrue(enforcement["executor_success"])
        self.assertIsNone(enforcement["error_category"])

    def test_registered_user_outcome_is_recorded_without_user_id(self):
        record = self.make_record()

        with isolated_fixture_tool_executor("C02") as provider:
            result = execute_tool_with_recording(
                record,
                "track_india_post",
                {"consignment_number": VALID_CONSIGNMENT},
                provider,
                user_context={"user_id": 123},
            )

        enforcement = record["python_enforcement"]

        self.assertTrue(result["success"])
        self.assertEqual(
            enforcement["outcome"],
            "allowed_and_executed",
        )
        self.assertEqual(
            enforcement["caller_role"],
            "registered_user",
        )
        self.assertNotIn("user_id", enforcement)

    def test_permission_denial_is_recorded(self):
        record = self.make_record()

        restricted_config = {
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

        with isolated_fixture_tool_executor("C02") as provider:
            result = execute_tool_with_recording(
                record,
                "track_india_post",
                {"consignment_number": VALID_CONSIGNMENT},
                provider,
                assistant_config=restricted_config,
            )

        enforcement = record["python_enforcement"]

        self.assertFalse(result["success"])
        self.assertEqual(
            enforcement["outcome"],
            "permission_denied",
        )
        self.assertEqual(
            enforcement["error_category"],
            "permission_denied",
        )
        self.assertFalse(enforcement["function_invoked"])
        self.assertEqual(enforcement["fixture_call_count"], 0)

    def test_unknown_tool_is_recorded(self):
        record = self.make_record()

        with isolated_fixture_tool_executor("C02") as provider:
            result = execute_tool_with_recording(
                record,
                "unknown_tool",
                {"consignment_number": VALID_CONSIGNMENT},
                provider,
            )

        enforcement = record["python_enforcement"]

        self.assertFalse(result["success"])
        self.assertEqual(enforcement["outcome"], "unknown_tool")
        self.assertFalse(enforcement["registered_tool"])
        self.assertFalse(enforcement["function_invoked"])

    def test_model_supplied_user_id_is_recorded_as_invalid_arguments(self):
        record = self.make_record()

        with isolated_fixture_tool_executor("C02") as provider:
            result = execute_tool_with_recording(
                record,
                "track_india_post",
                {
                    "consignment_number": VALID_CONSIGNMENT,
                    "user_id": 123,
                },
                provider,
            )

        enforcement = record["python_enforcement"]

        self.assertFalse(result["success"])
        self.assertEqual(
            enforcement["outcome"],
            "invalid_arguments",
        )
        self.assertEqual(
            enforcement["error_category"],
            "invalid_arguments",
        )
        self.assertCountEqual(
            enforcement["argument_keys"],
            ["consignment_number", "user_id"],
        )
        self.assertFalse(enforcement["function_invoked"])

    def test_invalid_trusted_context_is_recorded(self):
        record = self.make_record()

        with isolated_fixture_tool_executor("C02") as provider:
            result = execute_tool_with_recording(
                record,
                "track_india_post",
                {"consignment_number": VALID_CONSIGNMENT},
                provider,
                user_context={"user_id": True},
            )

        enforcement = record["python_enforcement"]

        self.assertFalse(result["success"])
        self.assertEqual(
            enforcement["outcome"],
            "invalid_user_context",
        )
        self.assertEqual(
            enforcement["error_category"],
            "invalid_user_context",
        )
        self.assertFalse(enforcement["function_invoked"])

    def test_enforcement_record_does_not_contain_sensitive_values(self):
        record = self.make_record()

        with isolated_fixture_tool_executor("C02") as provider:
            execute_tool_with_recording(
                record,
                "track_india_post",
                {"consignment_number": VALID_CONSIGNMENT},
                provider,
                user_context={"user_id": 123},
            )

        serialized = serialize_evaluation_records([record])

        self.assertNotIn(VALID_CONSIGNMENT, serialized)
        self.assertNotIn('"user_id"', serialized)
        self.assertNotIn('"123"', serialized)

    def test_model_decision_remains_separate_from_python_outcome(self):
        record = self.make_record()

        with isolated_fixture_tool_executor("C02") as provider:
            execute_tool_with_recording(
                record,
                "track_india_post",
                {"consignment_number": VALID_CONSIGNMENT},
                provider,
            )

        self.assertIsNone(record["actual_model_decision"])
        self.assertEqual(
            record["python_enforcement"]["outcome"],
            "allowed_and_executed",
        )

    def test_recording_requires_evaluation_dictionary(self):
        with isolated_fixture_tool_executor("C02") as provider:
            with self.assertRaises(TypeError):
                execute_tool_with_recording(
                    None,
                    "track_india_post",
                    {"consignment_number": VALID_CONSIGNMENT},
                    provider,
                )

class TestStage7Step6EvaluationReportIntegrity(unittest.TestCase):

    def make_complete_report(self):
        specifications = load_scenario_specifications()

        records = [
            create_evaluation_record(
                scenario_id=item["id"],
                user_input=item["user_input"],
                model_identifier="offline-mock",
                expected_decision=item["expected_decision"],
                synthetic_fixture=None,
            )
            for item in specifications
        ]
        for record in records:
            if record["scenario_id"] in {"F07", "F08", "F09", "F10"}:
                record["actual_model_decision"] = "not_applicable_python_boundary"
                record["python_enforcement"]["outcome"] = "python_probe_recorded"
        return records

    def test_all_39_scenario_records_validate(self):
        records = self.make_complete_report()

        result = validate_evaluation_records(records)

        self.assertTrue(result["valid"])
        self.assertEqual(result["record_count"], 39)

    def test_missing_scenario_record_is_rejected(self):
        records = self.make_complete_report()
        records.pop()

        with self.assertRaises(ValueError):
            validate_evaluation_records(records)

    def test_duplicate_scenario_id_is_rejected(self):
        records = self.make_complete_report()
        records[1]["scenario_id"] = records[0]["scenario_id"]

        with self.assertRaises(ValueError):
            validate_evaluation_records(records)

    def test_incorrect_scenario_order_is_rejected(self):
        records = self.make_complete_report()
        records[0], records[1] = records[1], records[0]

        with self.assertRaises(ValueError):
            validate_evaluation_records(records)

    def test_missing_record_field_is_rejected(self):
        records = self.make_complete_report()
        del records[0]["model_identifier"]

        with self.assertRaises(ValueError):
            validate_evaluation_records(records)

    def test_missing_python_enforcement_outcome_is_rejected(self):
        records = self.make_complete_report()
        records[0]["python_enforcement"]["outcome"] = None

        with self.assertRaises(ValueError):
            validate_evaluation_records(records)

    def test_model_and_python_results_remain_separate(self):
        records = self.make_complete_report()

        function_call = {
            "type": "function_call",
            "call_id": "separate-layer-test-call",
            "name": "track_india_post",
            "arguments": '{"consignment_number": "AB123456789IN"}',
        }
        response = {
            "id": "separate-layer-test-response",
            "output": [function_call],
        }
        records[0]["actual_model_decision"] = "tool_call"
        records[0]["tool_calls"] = [{
            "response_id": "separate-layer-test-response",
            "call_id": "separate-layer-test-call",
            "tool_name": "track_india_post",
            "arguments": '{"consignment_number": "AB123456789IN"}',
        }]
        records[0]["model_api_calls"] = [{
            "request": {"model": "offline-mock"},
            "forwarded": True,
            "failed": False,
            "budget_blocked": False,
            "response": response,
        }]
        records[0]["python_enforcement"] = {
            "outcome": "allowed_and_executed",
            "fixture_call_count": 1,
            "function_invoked": True,
            "executor_success": True,
        }

        result = validate_evaluation_records(records)

        self.assertTrue(result["valid"])
        self.assertEqual(
            records[0]["actual_model_decision"],
            "tool_call",
        )
        self.assertEqual(
            records[0]["python_enforcement"]["outcome"],
            "allowed_and_executed",
        )

    def test_model_decision_must_match_captured_function_calls(self):
        records = self.make_complete_report()
        records[0]["actual_model_decision"] = "tool_call"
        with self.assertRaises(ValueError):
            validate_evaluation_records(records)

    def test_tool_calls_must_match_api_response_output(self):
        records = self.make_complete_report()
        records[0]["tool_calls"] = [{
            "response_id": "r1",
            "call_id": "c1",
            "tool_name": "track_india_post",
            "arguments": "{}",
        }]
        with self.assertRaises(ValueError):
            validate_evaluation_records(records)

    def test_budget_blocked_attempt_cannot_be_marked_forwarded(self):
        records = self.make_complete_report()
        records[0]["model_api_calls"] = [{
            "request": {"model": "mock"},
            "forwarded": True,
            "failed": False,
            "budget_blocked": True,
            "error": {"type": "APIRequestBudgetExceeded"},
        }]
        with self.assertRaises(ValueError):
            validate_evaluation_records(records)

    def test_d02_accepts_deterministic_conflict_response_override(self):
        records = self.make_complete_report()
        record = next(item for item in records if item["scenario_id"] == "D02")
        record["actual_model_decision"] = "no_tool"
        record["model_api_calls"] = [{
            "request": {"model": "offline-mock"},
            "forwarded": True,
            "failed": False,
            "budget_blocked": False,
            "response": {
                "id": "response-d02",
                "output": [],
                "output_text": "The current status is In Transit.",
            },
            "response_output_text": "The current status is In Transit.",
        }]
        record["final_response"] = (
            "Data consistency note: The current-status field reports "
            "'InTransit', while a checkpoint reports 'Delivered' on "
            "13 November 2026 at Bengaluru Sorting Office. These records "
            "conflict, and the available data does not establish which value "
            "is authoritative. I cannot confirm the shipment's current status "
            "from this response."
        )

        result = validate_evaluation_records(records)
        self.assertTrue(result["valid"])

    def test_d03_rejects_speculative_unavailable_data_override(self):
        records = self.make_complete_report()
        record = next(item for item in records if item["scenario_id"] == "D03")
        record["actual_model_decision"] = "no_tool"
        record["model_api_calls"] = [{
            "request": {"model": "offline-mock"},
            "forwarded": True,
            "failed": False,
            "budget_blocked": False,
            "response": {
                "id": "response-d03",
                "output": [],
                "output_text": "Maybe the system has not updated.",
            },
            "response_output_text": "Maybe the system has not updated.",
        }]
        record["final_response"] = (
            "Availability note: The tracking lookup returned no tracking "
            "information. This might mean that the system has not updated. "
            "This does not establish the shipment's current status."
        )

        with self.assertRaises(ValueError):
            validate_evaluation_records(records)

    def test_final_response_must_match_last_captured_model_response(self):
        records = self.make_complete_report()
        records[0]["model_api_calls"] = [{
            "request": {"model": "mock"},
            "forwarded": True,
            "failed": False,
            "budget_blocked": False,
            "response": {
                "id": "response-1",
                "output": [],
                "output_text": "Captured answer",
            },
        }]
        records[0]["actual_model_decision"] = "no_tool"
        records[0]["final_response"] = "Different answer"
        with self.assertRaises(ValueError):
            validate_evaluation_records(records)

    def test_validator_uses_explicitly_captured_sdk_output_text(self):
        records = self.make_complete_report()
        records[0]["model_api_calls"] = [{
            "request": {"model": "mock"},
            "forwarded": True,
            "failed": False,
            "budget_blocked": False,
            "response": {
                "id": "response-sdk",
                "output": [],
            },
            "response_output_text": "Captured SDK property text",
        }]
        records[0]["actual_model_decision"] = "no_tool"
        records[0]["final_response"] = "Captured SDK property text"

        result = validate_evaluation_records(records)
        self.assertTrue(result["valid"])

    def test_json_serialization_round_trip_preserves_report(self):
        records = self.make_complete_report()

        serialized = serialize_evaluation_records(records)
        restored = json.loads(serialized)

        self.assertEqual(len(restored), 39)
        self.assertEqual(
            [item["scenario_id"] for item in restored],
            [item["scenario_id"] for item in records],
        )
        self.assertEqual(
            restored[0]["python_enforcement"]["outcome"],
            "not_evaluated",
        )

    def test_serializer_rejects_incomplete_report(self):
        records = self.make_complete_report()
        records.pop()

        with self.assertRaises(ValueError):
            serialize_evaluation_records(records)

    def test_serializer_rejects_non_list_input(self):
        with self.assertRaises(TypeError):
            serialize_evaluation_records({})


class TestStage7Step6OpenAIToolRecordingAdapter(unittest.TestCase):

    def test_adapter_is_injected_without_mutating_openai_agent(self):
        from ai_tools import openai_agent

        record = create_evaluation_record(
            scenario_id="C02",
            user_input="Track AB123456789IN",
            model_identifier="offline-mock",
            expected_decision="explain_evidence",
        )
        original_execute_tool = openai_agent.execute_tool

        with isolated_fixture_tool_executor("C02") as provider:
            injected_executor = create_fixture_tool_executor(record, provider)
            result = injected_executor(
                "track_india_post",
                '{"consignment_number": "AB123456789IN"}',
            )
            self.assertTrue(result["success"])
            self.assertEqual(
                record["python_enforcement"]["outcome"],
                "allowed_and_executed",
            )
            self.assertEqual(
                record["python_enforcement"]["fixture_call_count"],
                1,
            )

        self.assertIs(openai_agent.execute_tool, original_execute_tool)

    def test_fixture_executor_is_independent_per_provider(self):
        first_record = create_evaluation_record(
            "C02", "Track AB123456789IN", "offline-mock", "explain_evidence"
        )
        second_record = create_evaluation_record(
            "C03", "Track AB123456789IN", "offline-mock", "explain_evidence"
        )
        with isolated_fixture_tool_executor("C02") as first_provider:
            with isolated_fixture_tool_executor("C03") as second_provider:
                first_executor = create_fixture_tool_executor(first_record, first_provider)
                second_executor = create_fixture_tool_executor(second_record, second_provider)
                first_executor("track_india_post", {"consignment_number": VALID_CONSIGNMENT})
                second_executor("track_india_post", {"consignment_number": VALID_CONSIGNMENT})

        self.assertEqual(len(first_provider.calls), 1)
        self.assertEqual(len(second_provider.calls), 1)
        self.assertEqual(first_provider.fixture_key, "delivered")
        self.assertEqual(second_provider.fixture_key, "exception")

    def test_blocking_executor_is_a_callable_not_a_global_patch(self):
        from ai_tools import openai_agent
        record = create_evaluation_record(
            "A04", "Hello", "offline-mock", "no_tool"
        )
        original_execute_tool = openai_agent.execute_tool
        blocker = create_blocking_tool_executor(record, "offline")
        result = blocker("track_india_post", {"consignment_number": VALID_CONSIGNMENT})
        self.assertFalse(result["success"])
        self.assertEqual(
            record["python_enforcement"]["outcome"],
            "unexpected_tool_call_blocked",
        )
        self.assertIs(openai_agent.execute_tool, original_execute_tool)


class TestStage7Step6OfflineEvaluationRunner(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.result = run_offline_evaluation()

    def test_runner_creates_exactly_39_ordered_records(self):
        records = self.result["records"]

        self.assertEqual(len(records), 39)

        expected_ids = [
            item["id"]
            for item in load_scenario_specifications()
        ]

        self.assertEqual(
            [record["scenario_id"] for record in records],
            expected_ids,
        )

    def test_complete_report_validates_and_serializes(self):
        self.assertTrue(self.result["validation"]["valid"])

        restored = json.loads(self.result["report_json"])

        self.assertEqual(len(restored), 39)

    def test_semantic_scores_remain_pending(self):
        for record in self.result["records"]:
            with self.subTest(scenario_id=record["scenario_id"]):
                for score in record["scores"].values():
                    self.assertIsNone(score)

        self.assertTrue(
            self.result["summary"]["semantic_scores_pending"]
        )

    def test_tracking_scenarios_use_fixture_backed_executor(self):
        expected_tracking_ids = {
            "A01", "A02", "A03", "B05",
            "C01", "C02", "C03", "C04", "C05", "C06",
            "D01", "D02", "D03", "D04", "D05", "D06",
            "E01", "E02", "E04",
        }

        records = {
            record["scenario_id"]: record
            for record in self.result["records"]
        }

        for scenario_id in expected_tracking_ids:
            with self.subTest(scenario_id=scenario_id):
                record = records[scenario_id]

                self.assertEqual(
                    record["actual_model_decision"],
                    "tool_call",
                )
                expected_outcomes = {
                    "D03": "executed_but_tool_failed",
                    "D04": "executed_but_tool_failed",
                    "E04": "executed_but_tool_failed",
                }

                expected_outcome = expected_outcomes.get(
                    scenario_id,
                    "allowed_and_executed",
                )

                self.assertEqual(
                    record["python_enforcement"]["outcome"],
                    expected_outcome,
                )
                self.assertEqual(
                    record["python_enforcement"]["fixture_call_count"],
                    1,
                )
                self.assertEqual(len(record["tool_calls"]), 1)

    def test_no_tool_scenarios_do_not_invoke_tracking(self):
        no_tool_ids = {
            "A04", "A05", "A06",
            "B01", "B02", "B03", "B04", "B06",
            "E03", "E05",
            "F01", "F02", "F03", "F04", "F05", "F06",
        }

        records = {
            record["scenario_id"]: record
            for record in self.result["records"]
        }

        for scenario_id in no_tool_ids - {
            "F01", "F02", "F03", "F04", "F05", "F06"
        }:
            with self.subTest(scenario_id=scenario_id):
                record = records[scenario_id]

                self.assertEqual(
                    record["actual_model_decision"],
                    "no_tool",
                )
                self.assertEqual(record["tool_calls"], [])

        for scenario_id in no_tool_ids:
            self.assertEqual(
                records[scenario_id]["python_enforcement"]["outcome"],
                "not_applicable_no_tool_expected",
            )

    def test_security_scenarios_are_rejected_by_python(self):
        expected_outcomes = {
            "F07": "invalid_arguments",
            "F08": "unknown_tool",
            "F09": "permission_denied",
            "F10": "permission_denied",
        }

        records = {
            record["scenario_id"]: record
            for record in self.result["records"]
        }

        for scenario_id, expected_outcome in expected_outcomes.items():
            with self.subTest(scenario_id=scenario_id):
                record = records[scenario_id]

                self.assertEqual(
                    record["actual_model_decision"],
                    "not_applicable_python_boundary",
                )
                self.assertEqual(
                    record["python_enforcement"]["outcome"],
                    expected_outcome,
                )
                self.assertFalse(
                    record["python_enforcement"]["function_invoked"]
                )

    def test_conversation_context_is_supplied_without_database(self):
        records = {
            record["scenario_id"]: record
            for record in self.result["records"]
        }

        b05 = records["B05"]

        self.assertEqual(
            len(b05["conversation_context"]),
            2,
        )

        first_request = b05["model_api_calls"][0]["request"]
        request_input = first_request["input"]

        self.assertIn("Track AB123456789IN", request_input)
        self.assertIn("Where is my parcel now?", request_input)

    def test_e05_summarizes_supplied_prior_tracking_evidence_without_tool(self):
        record = next(
            item for item in self.result["records"]
            if item["scenario_id"] == "E05"
        )

        self.assertEqual(record["tool_calls"], [])
        self.assertIn("previously retrieved synthetic result", record["final_response"].lower())
        self.assertIn("12 November 2026", record["final_response"])

        scenario = next(
            item for item in load_scenario_specifications()
            if item["id"] == "E05"
        )
        assistant_context = [
            message["content"] for message in scenario["conversation_context"]
            if message["role"] == "assistant"
        ]
        self.assertEqual(len(assistant_context), 1)
        self.assertIn("Bengaluru Sorting Office", assistant_context[0])

    def test_runner_records_model_requests_and_responses(self):
        records = {
            record["scenario_id"]: record
            for record in self.result["records"]
        }

        a01 = records["A01"]

        self.assertEqual(len(a01["model_api_calls"]), 2)

        for call in a01["model_api_calls"]:
            self.assertIn("request", call)
            self.assertIn("response", call)

    def test_runner_reports_no_execution_errors(self):
        self.assertEqual(
            self.result["summary"]["error_count"],
            0,
        )

        for record in self.result["records"]:
            with self.subTest(scenario_id=record["scenario_id"]):
                self.assertEqual(record["errors"], [])

    def test_runner_uses_offline_mode(self):
        self.assertEqual(self.result["mode"], "offline_mock")

    def test_empty_model_identifier_is_rejected(self):
        with self.assertRaises(ValueError):
            run_offline_evaluation("")


class TestStage7Step6RecordingResponsesFailures(unittest.TestCase):

    def test_successful_call_records_request_and_response(self):
        class FakeResponses:
            def create(self, **kwargs):
                return {"id": "response_test_001", "status": "completed"}

        recorder = RecordingResponses(FakeResponses())

        result = recorder.create(
            model="test-model",
            input="Test evaluation request"
        )

        self.assertEqual(
            result,
            {"id": "response_test_001", "status": "completed"}
        )
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(
            recorder.calls[0]["request"]["model"],
            "test-model"
        )
        self.assertEqual(
            recorder.calls[0]["response"],
            result
        )
        self.assertNotIn("error", recorder.calls[0])

    def test_sdk_output_text_property_is_captured_when_model_dump_omits_it(self):
        class SDKStyleResponse:
            def __init__(self):
                self.id = "response_sdk_style"
                self.output = []
                self._output_text = "Captured through SDK property"

            @property
            def output_text(self):
                return self._output_text

            def model_dump(self):
                # Mirrors SDK objects whose computed output_text property
                # is not part of their serialized model fields.
                return {"id": self.id, "output": self.output}

        class FakeResponses:
            def create(self, **kwargs):
                return SDKStyleResponse()

        recorder = RecordingResponses(FakeResponses())
        result = recorder.create(model="test-model", input="Test")

        self.assertEqual(result.output_text, "Captured through SDK property")
        self.assertEqual(
            recorder.calls[0]["response"],
            {"id": "response_sdk_style", "output": []},
        )
        self.assertEqual(
            recorder.calls[0]["response_output_text"],
            "Captured through SDK property",
        )

    def test_dict_response_output_text_is_captured_separately(self):
        class FakeResponses:
            def create(self, **kwargs):
                return {
                    "id": "response_dict_style",
                    "output": [],
                    "output_text": "Dictionary response text",
                }

        recorder = RecordingResponses(FakeResponses())
        recorder.create(model="test-model", input="Test")

        self.assertEqual(
            recorder.calls[0]["response_output_text"],
            "Dictionary response text",
        )

    def test_failed_call_records_request_and_exception(self):
        class FakeResponses:
            def create(self, **kwargs):
                raise RuntimeError("Simulated API failure")

        recorder = RecordingResponses(FakeResponses())

        with self.assertRaises(RuntimeError):
            recorder.create(
                model="test-model",
                input="Test evaluation request"
            )

        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(
            recorder.calls[0]["request"]["model"],
            "test-model"
        )
        self.assertEqual(
            recorder.calls[0]["error"]["type"],
            "RuntimeError"
        )
        self.assertEqual(
            recorder.calls[0]["error"]["message"],
            "Simulated API failure"
        )
        self.assertNotIn("response", recorder.calls[0])

    def test_original_exception_object_is_reraised(self):
        original_exception = RuntimeError("Original exception")

        class FakeResponses:
            def create(self, **kwargs):
                raise original_exception

        recorder = RecordingResponses(FakeResponses())

        with self.assertRaises(RuntimeError) as context:
            recorder.create(model="test-model", input="Test")

        self.assertIs(context.exception, original_exception)

    def test_failed_call_does_not_prevent_later_successful_call(self):
        class FakeResponses:
            def __init__(self):
                self.call_count = 0

            def create(self, **kwargs):
                self.call_count += 1

                if self.call_count == 1:
                    raise RuntimeError("First attempt failed")

                return {"id": "response_test_002", "status": "completed"}

        fake_responses = FakeResponses()
        recorder = RecordingResponses(fake_responses)

        with self.assertRaises(RuntimeError):
            recorder.create(model="test-model", input="First request")

        result = recorder.create(
            model="test-model",
            input="Second request"
        )

        self.assertEqual(len(recorder.calls), 2)
        self.assertIn("error", recorder.calls[0])
        self.assertIn("response", recorder.calls[1])
        self.assertEqual(
            result["id"],
            "response_test_002"
        )


class TestStage7Step6RealModelConfiguration(unittest.TestCase):

    def test_valid_configuration_requires_explicit_confirmation(self):
        result = validate_real_model_evaluation_config(
            model_identifier="test-model",
            timeout_seconds=45,
            confirm_live_api_usage=True,
        )

        self.assertTrue(result["valid"])
        self.assertEqual(result["model_identifier"], "test-model")
        self.assertEqual(result["timeout_seconds"], 45)
        self.assertTrue(result["live_api_usage_confirmed"])
        self.assertEqual(result["scenario_count"], 39)
        self.assertGreater(result["fixture_count"], 0)

    def test_live_api_confirmation_is_required(self):
        with self.assertRaises(PermissionError):
            validate_real_model_evaluation_config(
                model_identifier="test-model"
            )

    def test_false_confirmation_is_rejected(self):
        with self.assertRaises(PermissionError):
            validate_real_model_evaluation_config(
                model_identifier="test-model",
                confirm_live_api_usage=False,
            )

    def test_non_boolean_confirmation_is_rejected(self):
        with self.assertRaises(PermissionError):
            validate_real_model_evaluation_config(
                model_identifier="test-model",
                confirm_live_api_usage=1,
            )

    def test_empty_model_identifier_is_rejected(self):
        for value in ("", "   ", None, 123):
            with self.subTest(model_identifier=value):
                with self.assertRaises(ValueError):
                    validate_real_model_evaluation_config(
                        model_identifier=value,
                        confirm_live_api_usage=True,
                    )

    def test_invalid_timeout_is_rejected(self):
        invalid_values = [
            0,
            -1,
            True,
            float("inf"),
            float("nan"),
            "30",
        ]

        for value in invalid_values:
            with self.subTest(timeout_seconds=value):
                with self.assertRaises(ValueError):
                    validate_real_model_evaluation_config(
                        model_identifier="test-model",
                        timeout_seconds=value,
                        confirm_live_api_usage=True,
                    )

    def test_model_identifier_is_trimmed(self):
        result = validate_real_model_evaluation_config(
            model_identifier="  test-model  ",
            confirm_live_api_usage=True,
        )

        self.assertEqual(result["model_identifier"], "test-model")

class TestStage7Step6RealModelScenarioRunner(unittest.TestCase):

    MODEL_IDENTIFIER = "test-real-model"

    @staticmethod
    def make_response(response_id, output=None, output_text=""):
        return SimpleNamespace(
            id=response_id,
            output=output or [],
            output_text=output_text,
        )

    @staticmethod
    def make_recording_client(responses):
        class FakeResponses:
            def __init__(self, response_items):
                self.response_items = iter(response_items)

            def create(self, **kwargs):
                response = next(self.response_items)

                if isinstance(response, Exception):
                    raise response

                return response

        fake_client = SimpleNamespace(
            responses=FakeResponses(responses)
        )

        return RecordingOpenAIClient(fake_client)

    def get_scenario(self, scenario_id):
        return next(
            item
            for item in load_scenario_specifications()
            if item["id"] == scenario_id
        )

    def test_tracking_scenario_uses_synthetic_fixture_and_real_executor(self):
        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({
                "consignment_number": VALID_CONSIGNMENT,
            }),
            call_id="test_call_C02",
        )

        client = self.make_recording_client([
            self.make_response(
                "response_C02_tool",
                output=[function_call],
            ),
            self.make_response(
                "response_C02_final",
                output_text="The synthetic shipment is marked delivered.",
            ),
        ])

        record = run_real_model_scenario(
            self.get_scenario("C02"),
            self.MODEL_IDENTIFIER,
            client,
        )

        self.assertEqual(record["scenario_id"], "C02")
        self.assertEqual(record["actual_model_decision"], "tool_call")
        self.assertEqual(len(record["tool_calls"]), 1)
        self.assertEqual(len(record["model_api_calls"]), 2)

        self.assertEqual(
            record["python_enforcement"]["outcome"],
            "allowed_and_executed",
        )

        self.assertEqual(
            record["python_enforcement"]["fixture_call_count"],
            1,
        )

        self.assertEqual(
            record["final_response"],
            "The synthetic shipment is marked delivered.",
        )

        self.assertTrue(record["errors"] == [])
        self.assertTrue(
            all(value is None for value in record["scores"].values())
        )
        self.assertIn(
            "DETERMINISTIC CONSIGNMENT-NUMBER VALIDATION",
            record["model_api_calls"][0]["request"]["instructions"],
        )
        self.assertIn(
            '"valid_13_character_candidates":["AB123456789IN"]',
            record["model_api_calls"][0]["request"]["instructions"],
        )

    def test_runner_passes_structured_user_history_to_prevalidation(self):
        specification = self.get_scenario("B05")
        client = self.make_recording_client([
            self.make_response(
                "response_B05",
                output_text="The previous shipment is being checked.",
            ),
        ])

        record = run_real_model_scenario(
            specification,
            self.MODEL_IDENTIFIER,
            client,
        )

        instructions = record["model_api_calls"][0]["request"]["instructions"]
        self.assertIn('"prior_user_messages"', instructions)
        self.assertIn('"valid_13_character_candidates":["AB123456789IN"]', instructions)
        self.assertIn('"current_user_message":{"candidate_count":0', instructions)

    def test_runner_excludes_assistant_history_from_candidate_inventory(self):
        specification = self.get_scenario("B05")
        specification["conversation_context"] = [
            {"role": "assistant", "content": "AB123456789IN"},
            {"role": "tool", "content": "CD987654321IN"},
        ]
        client = self.make_recording_client([
            self.make_response(
                "response_B05_no_user_candidate",
                output_text="Please provide the number.",
            ),
        ])

        record = run_real_model_scenario(
            specification,
            self.MODEL_IDENTIFIER,
            client,
        )
        instructions = record["model_api_calls"][0]["request"]["instructions"]
        self.assertNotIn('"historical_valid":["AB123456789IN"]', instructions)
        self.assertNotIn('"historical_valid":["CD987654321IN"]', instructions)

    def test_no_tool_scenario_records_no_tool_decision(self):
        client = self.make_recording_client([
            self.make_response(
                "response_A04",
                output_text="Hello! How can I help you?",
            ),
        ])

        record = run_real_model_scenario(
            self.get_scenario("A04"),
            self.MODEL_IDENTIFIER,
            client,
        )

        self.assertEqual(record["actual_model_decision"], "no_tool")
        self.assertEqual(record["tool_calls"], [])
        self.assertEqual(len(record["model_api_calls"]), 1)

        self.assertEqual(
            record["python_enforcement"]["outcome"],
            "not_applicable_no_tool_expected",
        )

        self.assertEqual(record["errors"], [])

    def test_unexpected_tool_call_is_blocked_and_recorded(self):
        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({
                "consignment_number": VALID_CONSIGNMENT,
            }),
            call_id="unexpected_call_A04",
        )

        client = self.make_recording_client([
            self.make_response(
                "response_A04_tool",
                output=[function_call],
            ),
            self.make_response(
                "response_A04_final",
                output_text="I can help with general questions.",
            ),
        ])

        record = run_real_model_scenario(
            self.get_scenario("A04"),
            self.MODEL_IDENTIFIER,
            client,
        )

        self.assertEqual(record["actual_model_decision"], "tool_call")
        self.assertEqual(len(record["tool_calls"]), 1)

        self.assertEqual(
            record["python_enforcement"]["outcome"],
            "unexpected_tool_call_blocked",
        )

        self.assertEqual(
            record["python_enforcement"]["fixture_call_count"],
            0,
        )

        self.assertTrue(
            any(
                error["error_type"] == "UnexpectedToolCall"
                for error in record["errors"]
            )
        )

    def test_api_exception_is_recorded_without_claiming_model_decision(self):
        client = self.make_recording_client([
            RuntimeError("Simulated API failure"),
        ])

        record = run_real_model_scenario(
            self.get_scenario("A04"),
            self.MODEL_IDENTIFIER,
            client,
        )

        self.assertIsNone(record["actual_model_decision"])
        self.assertIsNone(record["final_response"])
        self.assertEqual(len(record["model_api_calls"]), 1)

        self.assertIn(
            "error",
            record["model_api_calls"][0],
        )

        self.assertTrue(record["errors"])
        self.assertEqual(
            record["errors"][0]["error_type"],
            "OpenAIAgentError",
        )

        self.assertEqual(
            record["model_api_calls"][0]["error"]["type"],
            "RuntimeError",
        )

        self.assertNotEqual(
            record["python_enforcement"]["outcome"],
            "not_applicable_no_tool_expected",
        )

    def test_executor_is_restored_after_tracking_scenario(self):
        from ai_tools import openai_agent

        original_executor = openai_agent.execute_tool

        client = self.make_recording_client([
            self.make_response(
                "response_A04",
                output_text="Hello!",
            ),
        ])

        run_real_model_scenario(
            self.get_scenario("A04"),
            self.MODEL_IDENTIFIER,
            client,
        )

        self.assertIs(
            openai_agent.execute_tool,
            original_executor,
        )

    def test_api_calls_are_isolated_between_scenarios(self):
        client = self.make_recording_client([
            self.make_response(
                "response_A04_first",
                output_text="Hello!",
            ),
            self.make_response(
                "response_A04_second",
                output_text="Hello again!",
            ),
        ])

        first_record = run_real_model_scenario(
            self.get_scenario("A04"),
            self.MODEL_IDENTIFIER,
            client,
        )

        second_record = run_real_model_scenario(
            self.get_scenario("A04"),
            self.MODEL_IDENTIFIER,
            client,
        )

        self.assertEqual(len(first_record["model_api_calls"]), 1)
        self.assertEqual(len(second_record["model_api_calls"]), 1)

        self.assertEqual(
            first_record["model_api_calls"][0]["response"]["id"],
            "response_A04_first",
        )

        self.assertEqual(
            second_record["model_api_calls"][0]["response"]["id"],
            "response_A04_second",
        )

    def test_security_probe_scenarios_are_rejected(self):
        client = self.make_recording_client([])

        for scenario_id in ("F07", "F08", "F09", "F10"):
            with self.subTest(scenario_id=scenario_id):
                with self.assertRaises(ValueError):
                    run_real_model_scenario(
                        self.get_scenario(scenario_id),
                        self.MODEL_IDENTIFIER,
                        client,
                    )

    def test_runner_rejects_non_recording_client(self):
        with self.assertRaises(TypeError):
            run_real_model_scenario(
                self.get_scenario("A04"),
                self.MODEL_IDENTIFIER,
                object(),
            )


class TestStage7Step6RealModelEvaluationOrchestration(unittest.TestCase):

    MODEL_IDENTIFIER = "test-real-model"

    @staticmethod
    def make_recording_client():
        class FakeResponses:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                raise AssertionError(
                    "The orchestration test must not make API calls."
                )

        return RecordingOpenAIClient(
            SimpleNamespace(responses=FakeResponses())
        )

    def make_mock_scenario_record(
        self,
        specification,
        model_identifier,
    ):
        record = create_evaluation_record(
            scenario_id=specification["id"],
            user_input=specification["user_input"],
            model_identifier=model_identifier,
            expected_decision=specification["expected_decision"],
            synthetic_fixture=resolve_scenario_fixture(
                specification["id"]
            ),
        )

        record["conversation_context"] = specification.get(
            "conversation_context",
            [],
        )

        record["actual_model_decision"] = "no_tool"
        record["model_api_calls"] = [{
            "request": {"model": model_identifier},
            "forwarded": True,
            "failed": False,
            "budget_blocked": False,
            "response": {
                "id": f"mock_{specification['id']}",
                "output": [],
            },
        }]

        record["python_enforcement"] = {
            "outcome": "mocked_scenario_runner",
            "fixture_call_count": 0,
        }

        return record

    def test_orchestrator_builds_all_39_records_in_order(self):
        client = self.make_recording_client()

        specifications = load_scenario_specifications()

        def mock_scenario_runner(
            specification,
            model_identifier,
            recording_client,
        ):
            self.assertIs(recording_client, client)

            return self.make_mock_scenario_record(
                specification,
                model_identifier,
            )

        with patch(
            "stage7_step6_evaluation.run_real_model_scenario",
            side_effect=mock_scenario_runner,
        ) as scenario_runner:
            with patch(
                "stage7_step6_evaluation.build_real_model_recording_client",
                side_effect=AssertionError(
                    "An injected client must not create a real client."
                ),
            ):
                result = run_real_model_evaluation(
                    model_identifier=self.MODEL_IDENTIFIER,
                    confirm_live_api_usage=True,
                    recording_client=client,
                )

        self.assertEqual(
            scenario_runner.call_count,
            35,
        )

        self.assertEqual(
            result["mode"],
            "real_model",
        )

        self.assertEqual(
            result["validation"]["record_count"],
            39,
        )

        self.assertEqual(
            [record["scenario_id"] for record in result["records"]],
            [item["id"] for item in specifications],
        )

        self.assertEqual(
            result["summary"]["scenario_count"],
            39,
        )

        self.assertEqual(
            result["summary"]["model_scenario_count"],
            35,
        )

        self.assertEqual(
            result["summary"]["python_security_probe_count"],
            4,
        )

        self.assertEqual(
            result["summary"]["model_api_call_count"],
            35,
        )
        self.assertEqual(
            result["summary"]["api_request_attempt_count"],
            35,
        )
        self.assertEqual(
            result["summary"]["api_request_forwarded_count"],
            35,
        )
        self.assertEqual(
            result["summary"]["api_request_budget_blocked_count"],
            0,
        )
        self.assertEqual(
            result["summary"]["evaluation_status"],
            "completed_scores_pending",
        )
        self.assertFalse(
            result["summary"]["semantic_evaluation_passed"]
        )

        self.assertEqual(
            result["summary"]["error_count"],
            0,
        )

        self.assertTrue(
            result["summary"]["semantic_scores_pending"]
        )

        self.assertEqual(
            len(json.loads(result["report_json"])),
            39,
        )

        self.assertEqual(
            client.responses.calls,
            [],
        )

        for record in result["records"]:
            self.assertTrue(
                all(
                    value is None
                    for value in record["scores"].values()
                )
            )

    def test_request_budget_stops_later_model_scenarios(self):
        class FakeResponses:
            def create(self, **kwargs):
                return SimpleNamespace(
                    id="budget_test_response",
                    output=[],
                    output_text="mock response",
                )

        client = RecordingOpenAIClient(
            SimpleNamespace(responses=FakeResponses())
        )
        original_specs = load_scenario_specifications()
        calls = []

        def mock_scenario_runner(
            specification,
            model_identifier,
            recording_client,
        ):
            calls.append(specification["id"])
            recording_client.responses.create(model=model_identifier)
            return self.make_mock_scenario_record(
                specification,
                model_identifier,
            )

        with patch(
            "stage7_step6_evaluation.run_real_model_scenario",
            side_effect=mock_scenario_runner,
        ):
            result = run_real_model_evaluation(
                model_identifier=self.MODEL_IDENTIFIER,
                confirm_live_api_usage=True,
                recording_client=client,
                max_api_requests=1,
            )

        self.assertEqual(calls, ["A01"])
        self.assertEqual(result["summary"]["api_request_budget"], 1)
        self.assertEqual(result["summary"]["api_requests_used"], 1)
        self.assertTrue(result["summary"]["api_request_budget_exhausted"])
        self.assertEqual(
            result["records"][1]["python_enforcement"]["outcome"],
            "not_run_api_budget_exhausted",
        )
        self.assertEqual(len(result["records"]), 39)
        self.assertEqual(
            result["summary"]["evaluation_status"],
            "incomplete_budget_exhausted",
        )
        self.assertEqual(
            result["summary"]["model_scenarios_skipped_count"],
            34,
        )
        self.assertEqual(
            result["summary"]["api_request_attempt_count"],
            1,
        )
        self.assertEqual(
            result["summary"]["api_request_forwarded_count"],
            1,
        )

    def test_security_probes_run_without_model_calls(self):
        client = self.make_recording_client()

        specifications = load_scenario_specifications()

        def mock_scenario_runner(
            specification,
            model_identifier,
            recording_client,
        ):
            return self.make_mock_scenario_record(
                specification,
                model_identifier,
            )

        with patch(
            "stage7_step6_evaluation.run_real_model_scenario",
            side_effect=mock_scenario_runner,
        ):
            result = run_real_model_evaluation(
                model_identifier=self.MODEL_IDENTIFIER,
                confirm_live_api_usage=True,
                recording_client=client,
            )

        security_records = [
            record
            for record in result["records"]
            if record["scenario_id"] in {
                "F07", "F08", "F09", "F10"
            }
        ]

        self.assertEqual(len(security_records), 4)

        for record in security_records:
            self.assertEqual(
                record["actual_model_decision"],
                "not_applicable_python_boundary",
            )

            self.assertEqual(
                record["model_api_calls"],
                [],
            )

            self.assertEqual(
                record["tool_calls"],
                [],
            )

            self.assertEqual(
                record["python_enforcement"]["fixture_call_count"],
                0,
            )

            self.assertNotEqual(
                record["python_enforcement"]["outcome"],
                "not_evaluated",
            )

        self.assertEqual(client.responses.calls, [])

    def test_live_api_confirmation_is_required_before_execution(self):
        client = self.make_recording_client()

        with patch(
            "stage7_step6_evaluation.run_real_model_scenario"
        ) as scenario_runner:
            with patch(
                "stage7_step6_evaluation.build_real_model_recording_client"
            ) as client_builder:
                with self.assertRaises(PermissionError):
                    run_real_model_evaluation(
                        model_identifier=self.MODEL_IDENTIFIER,
                        confirm_live_api_usage=False,
                        recording_client=client,
                    )

        scenario_runner.assert_not_called()
        client_builder.assert_not_called()

    def test_real_client_builder_is_used_when_client_is_not_injected(self):
        client = self.make_recording_client()

        specifications = load_scenario_specifications()

        def mock_scenario_runner(
            specification,
            model_identifier,
            recording_client,
        ):
            return self.make_mock_scenario_record(
                specification,
                model_identifier,
            )

        with patch(
            "stage7_step6_evaluation.build_real_model_recording_client",
            return_value=client,
        ) as client_builder:
            with patch(
                "stage7_step6_evaluation.run_real_model_scenario",
                side_effect=mock_scenario_runner,
            ):
                result = run_real_model_evaluation(
                    model_identifier=self.MODEL_IDENTIFIER,
                    timeout_seconds=45,
                    confirm_live_api_usage=True,
                )

        client_builder.assert_called_once_with(
            self.MODEL_IDENTIFIER,
            timeout_seconds=45,
        )

        self.assertEqual(
            result["validation"]["record_count"],
            39,
        )

        self.assertEqual(client.responses.calls, [])


if __name__ == "__main__":
    unittest.main()

class TestCorrectiveShipmentResponseGuardrails(unittest.TestCase):
    """Regression coverage for findings from the real-model review."""

    @classmethod
    def setUpClass(cls):
        from ai_tools.assistant_config import resolve_instructions
        cls.instructions = resolve_instructions()

    def test_unknown_status_requires_evidence_based_uncertainty(self):
        self.assertIn(
            "available records do not establish the shipment status",
            self.instructions,
        )
        self.assertIn(
            "Do not speculate that the consignment is unregistered, lost",
            self.instructions,
        )

    def test_stale_checkpoint_requires_explicit_freshness_disclosure(self):
        self.assertIn(
            "disclose that the information may be stale",
            self.instructions,
        )
        self.assertIn("clearly identify its recorded date", self.instructions)

    def test_conflicting_tracking_evidence_must_be_disclosed(self):
        self.assertIn(
            "explicitly disclose the specific conflicting information",
            self.instructions,
        )
        self.assertIn(
            "do not allow you to determine which value is authoritative",
            self.instructions,
        )
        self.assertIn(
            "Do not silently choose one value",
            self.instructions,
        )

    def test_previous_provider_failure_does_not_invalidate_consignment(self):
        self.assertIn(
            "A previous provider or tracking-request failure does not make "
            "the consignment number invalid",
            self.instructions,
        )
        self.assertIn(
            "For a follow-up update that clearly refers to exactly one "
            "previously identified valid consignment number, reuse that number",
            self.instructions,
        )

    def test_unsupported_operations_are_declined_without_collecting_number(self):
        self.assertIn(
            "Do not offer to perform, imply that you can perform, or request "
            "a consignment number as a prerequisite for unsupported operations",
            self.instructions,
        )
        for capability in (
            "changing a delivery address",
            "cancelling or redirecting a parcel",
            "filing a complaint",
            "contacting delivery personnel",
            "predicting an exact delivery date",
        ):
            with self.subTest(capability=capability):
                self.assertIn(capability, self.instructions)

    def test_mandatory_existing_tracking_safeguards_remain_present(self):
        self.assertIn(
            "Do not invent, guess, complete, or silently alter a consignment number",
            self.instructions,
        )
        self.assertIn(
            "For requests unrelated to shipment tracking, do not call the tracking tool.",
            self.instructions,
        )


class TestMilestone2Stage7Step6658Corrections(unittest.TestCase):
    MODEL_IDENTIFIER = "offline-correction-test-model"

    def make_response(self, response_id, output=None, output_text=""):
        return SimpleNamespace(
            id=response_id,
            output=output or [],
            output_text=output_text,
        )

    def make_recording_client(self, responses):
        class FakeResponses:
            def __init__(self, items):
                self.items = iter(items)
            def create(self, **kwargs):
                return next(self.items)
        return RecordingOpenAIClient(
            SimpleNamespace(responses=FakeResponses(responses))
        )

    def get_scenario(self, scenario_id):
        return next(
            item for item in load_scenario_specifications()
            if item["id"] == scenario_id
        )

    def test_d01_stale_fixture_gets_mandatory_deterministic_disclosure(self):
        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({"consignment_number": VALID_CONSIGNMENT}),
            call_id="d01_stale_call",
        )
        client = self.make_recording_client([
            self.make_response("d01_tool", [function_call]),
            self.make_response(
                "d01_final",
                output_text=(
                    "The latest checkpoint was recorded on 2 November 2026."
                ),
            ),
        ])

        from unittest.mock import patch
        with patch(
            "ai_tools.openai_agent._india_local_date",
            return_value=__import__("datetime").date(2026, 11, 15),
        ):
            record = run_real_model_scenario(
                self.get_scenario("D01"),
                self.MODEL_IDENTIFIER,
                client,
            )

        self.assertEqual(record["python_enforcement"]["outcome"], "allowed_and_executed")
        self.assertTrue(
            record["final_response"].startswith(
                "The latest recorded checkpoint shows In Transit"
            )
        )
        self.assertIn("Delhi Sorting Office", record["final_response"])
        self.assertIn("2 November 2026", record["final_response"])
        self.assertIn("13 days old", record["final_response"])
        self.assertIn("may be stale", record["final_response"])
        self.assertIn(
            "does not confirm the shipment's current location or current status",
            record["final_response"],
        )
        self.assertNotIn("currently in transit", record["final_response"].lower())

    def test_e05_prior_tracking_evidence_is_in_model_input_and_no_tool_is_expected(self):
        client = self.make_recording_client([
            self.make_response(
                "e05_final",
                output_text=(
                    "The previously retrieved tracking result shows InTransit "
                    "at Bengaluru Sorting Office on 12 November 2026."
                ),
            ),
        ])

        record = run_real_model_scenario(
            self.get_scenario("E05"),
            self.MODEL_IDENTIFIER,
            client,
        )

        request_input = record["model_api_calls"][0]["request"]["input"]
        self.assertIn("Previously retrieved tracking result", request_input)
        self.assertIn("Bengaluru Sorting Office", request_input)
        self.assertIn("Current user message:", request_input)
        self.assertIn("Summarize the tracking information you already have.", request_input)
        self.assertEqual(record["tool_calls"], [])
        self.assertEqual(
            record["python_enforcement"]["outcome"],
            "not_applicable_no_tool_expected",
        )
        self.assertEqual(record["errors"], [])
        self.assertFalse(any(
            "no previous tracking result" in note.lower()
            for note in record["evaluator_notes"]
        ))

    def test_b04_unexpected_ambiguous_call_is_blocked_and_clarification_is_deterministic(self):
        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({"consignment_number": VALID_CONSIGNMENT}),
            call_id="b04_unexpected_call",
        )
        client = self.make_recording_client([
            self.make_response("b04_tool", [function_call]),
        ])

        record = run_real_model_scenario(
            self.get_scenario("B04"),
            self.MODEL_IDENTIFIER,
            client,
        )

        self.assertEqual(record["actual_model_decision"], "tool_call")
        self.assertEqual(
            record["python_enforcement"]["outcome"],
            "unexpected_tool_call_blocked",
        )
        self.assertEqual(record["python_enforcement"]["fixture_call_count"], 0)
        self.assertEqual(len(record["model_api_calls"]), 1)
        self.assertIn("more than one possible consignment number", record["final_response"].lower())
        self.assertIn("Which one would you like me to track?", record["final_response"])
        self.assertEqual(len(record["errors"]), 1)
        self.assertEqual(record["errors"][0]["error_type"], "UnexpectedToolCall")
