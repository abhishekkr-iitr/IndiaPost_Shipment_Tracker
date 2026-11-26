
import unittest
from unittest.mock import patch

from ai_tools.openai_agent import ask_openai
from ai_tools.tool_executor import execute_tool, AVAILABLE_TOOLS


# ---------------------------------------------------------------------
# Milestone 2 - Stage 7 - Step 5
# Grounded evaluation catalogue: all 39 approved scenarios.
#
# These are evaluation specifications. Their presence does not mean
# semantic model quality has been tested or passed.
# ---------------------------------------------------------------------

SCENARIOS = [
    # Group A - Intent recognition and tool selection
    {"id": "A01", "decision": "track"},
    {"id": "A02", "decision": "track"},
    {"id": "A03", "decision": "track"},
    {"id": "A04", "decision": "no_tool"},
    {"id": "A05", "decision": "no_tracking_tool"},
    {"id": "A06", "decision": "no_tracking_tool"},

    # Group B - Consignment extraction and clarification
    {"id": "B01", "decision": "clarify"},
    {"id": "B02", "decision": "clarify"},
    {"id": "B03", "decision": "clarify"},
    {"id": "B04", "decision": "clarify"},
    {"id": "B05", "decision": "reuse_if_unambiguous"},
    {"id": "B06", "decision": "clarify"},

    # Group C - Shipment-state evaluation
    {"id": "C01", "decision": "explain_evidence"},
    {"id": "C02", "decision": "explain_evidence"},
    {"id": "C03", "decision": "explain_evidence"},
    {"id": "C04", "decision": "explain_evidence"},
    {"id": "C05", "decision": "disclose_unknown"},
    {"id": "C06", "decision": "disclose_incomplete_data"},

    # Group D - Data freshness and reliability
    {"id": "D01", "decision": "disclose_staleness"},
    {"id": "D02", "decision": "disclose_conflict"},
    {"id": "D03", "decision": "disclose_unavailable_data"},
    {"id": "D04", "decision": "report_failure"},
    {"id": "D05", "decision": "disclose_incomplete_data"},
    {"id": "D06", "decision": "explain_new_checkpoint"},

    # Group E - Follow-up and conversation continuity
    {"id": "E01", "decision": "refresh_if_unambiguous"},
    {"id": "E02", "decision": "refresh_if_required"},
    {"id": "E03", "decision": "clarify"},
    {"id": "E04", "decision": "preserve_failure_state"},
    {"id": "E05", "decision": "summarize_available_evidence"},

    # Group F - Unsupported requests and security boundaries
    {"id": "F01", "decision": "unsupported"},
    {"id": "F02", "decision": "unsupported"},
    {"id": "F03", "decision": "unsupported"},
    {"id": "F04", "decision": "unsupported"},
    {"id": "F05", "decision": "unsupported"},
    {"id": "F06", "decision": "unsupported"},
    {"id": "F07", "decision": "python_rejection"},
    {"id": "F08", "decision": "python_rejection"},
    {"id": "F09", "decision": "python_permission_denial"},
    {"id": "F10", "decision": "python_permission_denial"},
]


def mock_tracking_tool(consignment_number, user_id=None):
    """Mock tracking implementation; never contacts a provider."""
    return {
        "success": True,
        "data": {
            "tracking_number": consignment_number,
            "current_status": "In Transit",
            "status_message": "Shipment is in transit",
            "checkpoints": [],
        },
        "received_user_id": user_id,
    }


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
    from types import SimpleNamespace

    return SimpleNamespace(
        id=response_id,
        output=output,
        output_text=output_text,
    )


class TestStage7Step5ScenarioCatalogue(unittest.TestCase):

    def test_catalogue_contains_exactly_39_scenarios(self):
        self.assertEqual(len(SCENARIOS), 39)

    def test_catalogue_contains_all_approved_scenario_ids_once(self):
        expected_ids = (
            [f"A{i:02d}" for i in range(1, 7)]
            + [f"B{i:02d}" for i in range(1, 7)]
            + [f"C{i:02d}" for i in range(1, 7)]
            + [f"D{i:02d}" for i in range(1, 7)]
            + [f"E{i:02d}" for i in range(1, 6)]
            + [f"F{i:02d}" for i in range(1, 11)]
        )

        actual_ids = [scenario["id"] for scenario in SCENARIOS]

        self.assertEqual(actual_ids, expected_ids)
        self.assertEqual(len(actual_ids), len(set(actual_ids)))

    def test_every_scenario_has_an_expected_decision(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario_id=scenario["id"]):
                self.assertTrue(scenario["decision"])



class TestStage7Step5AssistantOrchestration(unittest.TestCase):

    def test_clarification_does_not_invoke_tracking(self):
        client = MockOpenAIClient([
            make_response(
                "response_clarification",
                [],
                "Please provide the 13-character consignment number.",
            )
        ])

        with patch(
            "ai_tools.openai_agent.execute_tool"
        ) as mock_execute:
            result = ask_openai(
                "Please track my shipment",
                client=client,
                model="mock-model",
            )

        self.assertIn("consignment number", result.lower())
        mock_execute.assert_not_called()
        self.assertEqual(len(client.responses.calls), 1)

    def _assert_tracking_request_uses_exact_number(
        self,
        user_message,
        consignment_number,
        final_response,
    ):
        function_call = __import__("types").SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=__import__("json").dumps({
                "consignment_number": consignment_number,
            }),
            call_id="call_stage7",
        )

        client = MockOpenAIClient([
            make_response(
                "response_tracking_call",
                [function_call],
            ),
            make_response(
                "response_tracking_final",
                [],
                final_response,
            ),
        ])

        with patch(
            "ai_tools.openai_agent.execute_tool",
            return_value={
                "success": True,
                "data": {
                    "tracking_number": consignment_number,
                    "status": "In Transit",
                },
            },
        ) as mock_execute:
            result = ask_openai(
                user_message,
                client=client,
                model="mock-model",
            )

        self.assertEqual(result, final_response)

        mock_execute.assert_called_once_with(
            "track_india_post",
            function_call.arguments,
        )

        arguments = __import__("json").loads(
            function_call.arguments
        )
        self.assertEqual(
            arguments["consignment_number"],
            consignment_number,
        )

        self.assertEqual(len(client.responses.calls), 2)

        tool_output = client.responses.calls[1]["input"][0]
        self.assertEqual(
            tool_output["type"],
            "function_call_output",
        )
        self.assertEqual(
            tool_output["call_id"],
            "call_stage7",
        )

        returned_result = __import__("json").loads(
            tool_output["output"]
        )
        self.assertTrue(returned_result["success"])
        self.assertEqual(
            returned_result["data"]["tracking_number"],
            consignment_number,
        )

    def test_A01_tracking_request_invokes_exact_consignment(self):
        self._assert_tracking_request_uses_exact_number(
            "Track AB123456789IN",
            "AB123456789IN",
            "The tracking result is available.",
        )

    def test_A02_delivery_status_request_invokes_exact_consignment(self):
        self._assert_tracking_request_uses_exact_number(
            "What is the delivery status of AB123456789IN?",
            "AB123456789IN",
            "The recorded status is In Transit.",
        )

    def test_A03_tracking_history_request_invokes_exact_consignment(self):
        self._assert_tracking_request_uses_exact_number(
            "Show the tracking history of AB123456789IN",
            "AB123456789IN",
            "The tracking history is available.",
        )


    def _assert_no_tracking_for_general_message(
        self,
        user_message,
        final_response,
    ):
        client = MockOpenAIClient([
            make_response(
                "response_general",
                [],
                final_response,
            )
        ])

        with patch(
            "ai_tools.openai_agent.execute_tool"
        ) as mock_execute:
            result = ask_openai(
                user_message,
                client=client,
                model="mock-model",
            )

        self.assertEqual(result, final_response)
        mock_execute.assert_not_called()
        self.assertEqual(len(client.responses.calls), 1)

    def test_A04_greeting_does_not_invoke_tracking(self):
        self._assert_no_tracking_for_general_message(
            "Hello, how are you?",
            "Hello! How can I help you today?",
        )

    def test_A05_general_services_question_does_not_invoke_tracking(self):
        self._assert_no_tracking_for_general_message(
            "What services does India Post provide?",
            "I can help with shipment tracking. For authoritative "
            "information about other services, please consult India Post.",
        )

    def test_A06_general_in_transit_explanation_does_not_invoke_tracking(self):
        self._assert_no_tracking_for_general_message(
            "Explain what an in-transit status means",
            "In transit generally means that a shipment is recorded "
            "as moving through the delivery network.",
        )


    def test_F01_delivery_address_change_is_unsupported(self):
        self._assert_no_tracking_for_general_message(
            "Change the delivery address of my parcel.",
            "I cannot change a parcel's delivery address.",
        )

    def test_F02_cancellation_or_redirection_is_unsupported(self):
        self._assert_no_tracking_for_general_message(
            "Cancel or redirect my parcel.",
            "I cannot cancel or redirect a parcel.",
        )

    def test_F03_complaint_filing_is_unsupported(self):
        self._assert_no_tracking_for_general_message(
            "File a complaint with India Post for my parcel.",
            "I cannot file a complaint with India Post on your behalf.",
        )

    def test_F04_contacting_delivery_personnel_is_unsupported(self):
        self._assert_no_tracking_for_general_message(
            "Contact the delivery person and ask about my parcel.",
            "I cannot contact delivery personnel on your behalf.",
        )

    def test_F05_exact_delivery_date_prediction_is_unsupported(self):
        self._assert_no_tracking_for_general_message(
            "Predict the exact delivery date of my parcel.",
            "I cannot reliably predict an exact delivery date.",
        )

    def test_F06_notifications_and_monitoring_are_unsupported(self):
        self._assert_no_tracking_for_general_message(
            "Enable notifications and monitor my parcel automatically.",
            "I cannot enable notifications or scheduled shipment monitoring.",
        )

    def _assert_tracking_evidence_is_forwarded(
        self,
        scenario_id,
        user_message,
        tracking_data,
        final_response,
    ):
        import json
        from types import SimpleNamespace

        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({
                "consignment_number": tracking_data["tracking_number"],
            }),
            call_id=f"call_{scenario_id}",
        )

        client = MockOpenAIClient([
            make_response(
                f"response_{scenario_id}_call",
                [function_call],
            ),
            make_response(
                f"response_{scenario_id}_final",
                [],
                final_response,
            ),
        ])

        tool_result = {
            "success": True,
            "data": tracking_data,
        }

        with patch(
            "ai_tools.openai_agent.execute_tool",
            return_value=tool_result,
        ) as mock_execute:
            result = ask_openai(
                user_message,
                client=client,
                model="mock-model",
            )

        latest_checkpoint_dates = [
            checkpoint.get("date")
            for checkpoint in tracking_data.get("checkpoints", [])
            if isinstance(checkpoint, dict) and checkpoint.get("date")
        ]
        if latest_checkpoint_dates:
            from datetime import datetime
            from ai_tools.openai_agent import _india_local_date
            parsed_dates = [
                datetime.fromisoformat(value.replace("Z", "+00:00")).date()
                for value in latest_checkpoint_dates
            ]
            latest_date = max(parsed_dates)
            age_days = (_india_local_date() - latest_date).days
        else:
            age_days = 0

        if age_days >= 13:
            if scenario_id == "D02":
                self.assertTrue(result.startswith("Data consistency note:"))
                self.assertIn("current-status field reports 'InTransit'", result)
                self.assertIn("checkpoint reports 'Delivered'", result)
                self.assertIn("records conflict", result)
                self.assertNotIn(final_response, result)
            else:
                self.assertTrue(
                    result.startswith("The latest recorded checkpoint shows ")
                )

            self.assertIn("may be stale", result)
            self.assertIn("last recorded checkpoint only", result)
            self.assertIn("does not confirm the shipment's current location or current status", result)
            self.assertNotIn("currently in transit", result.lower())
        elif scenario_id == "D02":
            self.assertTrue(result.startswith("Data consistency note:"))
            self.assertIn("current-status field reports 'InTransit'", result)
            self.assertIn("checkpoint reports 'Delivered'", result)
            self.assertIn("records conflict", result)
            self.assertNotIn(final_response, result)
        else:
            self.assertEqual(result, final_response)

        mock_execute.assert_called_once_with(
            "track_india_post",
            function_call.arguments,
        )

        self.assertEqual(len(client.responses.calls), 2)

        tool_output = client.responses.calls[1]["input"][0]
        self.assertEqual(
            tool_output["type"],
            "function_call_output",
        )
        self.assertEqual(
            tool_output["call_id"],
            function_call.call_id,
        )

        forwarded_result = json.loads(tool_output["output"])

        self.assertTrue(forwarded_result["success"])
        self.assertEqual(
            forwarded_result["data"],
            tracking_data,
        )


    def _assert_tracking_failure_is_forwarded(
        self,
        scenario_id,
        user_message,
        tool_result,
        final_response,
    ):
        import json
        from types import SimpleNamespace

        consignment_number = "AB123456789IN"

        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({
                "consignment_number": consignment_number,
            }),
            call_id=f"call_{scenario_id}",
        )

        client = MockOpenAIClient([
            make_response(
                f"response_{scenario_id}_call",
                [function_call],
            ),
            make_response(
                f"response_{scenario_id}_final",
                [],
                final_response,
            ),
        ])

        with patch(
            "ai_tools.openai_agent.execute_tool",
            return_value=tool_result,
        ) as mock_execute:
            result = ask_openai(
                user_message,
                client=client,
                model="mock-model",
            )

        if scenario_id == "D03":
            self.assertTrue(result.startswith("Availability note:"))
            self.assertIn("returned no tracking information", result)
            self.assertIn("does not establish that the consignment number is invalid", result)
            self.assertIn("does not establish the shipment's current status", result)
            self.assertNotIn("might mean", result.lower())
            self.assertNotIn("not been updated in the system", result.lower())
            self.assertNotIn(final_response, result)
        else:
            self.assertEqual(result, final_response)
        mock_execute.assert_called_once_with(
            "track_india_post",
            function_call.arguments,
        )

        tool_output = client.responses.calls[1]["input"][0]
        self.assertEqual(
            tool_output["type"],
            "function_call_output",
        )
        self.assertEqual(
            tool_output["call_id"],
            function_call.call_id,
        )

        forwarded_result = json.loads(tool_output["output"])
        self.assertEqual(forwarded_result, tool_result)


    def test_D03_no_tracking_information_is_forwarded_as_unavailable(self):
        self._assert_tracking_failure_is_forwarded(
            "D03",
            "Track AB123456789IN",
            {
                "success": False,
                "error": "No tracking information available.",
            },
            "Tracking information is currently unavailable.",
        )

    def test_D04_provider_failure_is_forwarded_as_unsuccessful(self):
        self._assert_tracking_failure_is_forwarded(
            "D04",
            "Track AB123456789IN",
            {
                "success": False,
                "error": "Tracking provider request failed.",
            },
            "The tracking request was unsuccessful. Please try again later.",
        )

    def test_D05_incomplete_tracking_data_is_preserved(self):
        self._assert_tracking_evidence_is_forwarded(
            "D05",
            "Track AB123456789IN",
            {
                "tracking_number": "AB123456789IN",
                "carrier": "india-post",
                "current_status": "InTransit",
                "status_message": None,
                "origin": None,
                "destination": "Bengaluru, India",
                "pickup_date": None,
                "delivery_date": None,
                "checkpoints": [
                    {
                        "date": "2026-11-14T10:00:00+05:30",
                        "location": None,
                        "status": "InTransit",
                        "message": None,
                    }
                ],
            },
            "The available tracking information is incomplete.",
        )

    def test_C01_in_transit_recent_checkpoint_evidence_forwarded(self):
        self._assert_tracking_evidence_is_forwarded(
            "C01",
            "Track AB123456789IN",
            {
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
            "The latest recorded checkpoint is available.",
        )

    def test_C02_delivered_status_and_date_evidence_forwarded(self):
        self._assert_tracking_evidence_is_forwarded(
            "C02",
            "Track AB123456789IN",
            {
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
            "The recorded shipment status is Delivered.",
        )

    def test_C03_exception_checkpoint_evidence_forwarded(self):
        self._assert_tracking_evidence_is_forwarded(
            "C03",
            "Track AB123456789IN",
            {
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
            "A delivery exception is recorded in the tracking information.",
        )

    def test_C04_explicit_delay_evidence_forwarded(self):
        self._assert_tracking_evidence_is_forwarded(
            "C04",
            "Track AB123456789IN",
            {
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
            "The tracking information records a delay.",
        )

    def test_C05_unknown_status_evidence_forwarded(self):
        self._assert_tracking_evidence_is_forwarded(
            "C05",
            "Track AB123456789IN",
            {
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
            (
            "Unknown-status note: The tracking result reports an unknown or "
            "unrecognized status ('Unknown'). No usable status message or "
            "checkpoint details are available in this result. The available "
            "records do not establish the shipment's current status. This "
            "does not establish that the consignment is unregistered, lost, "
            "or has not moved."
        ),
        )

    def test_C06_incomplete_tracking_information_forwarded(self):
        self._assert_tracking_evidence_is_forwarded(
            "C06",
            "Track AB123456789IN",
            {
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
            "The available tracking information is incomplete.",
        )


    def test_D01_old_checkpoint_timestamp_is_forwarded(self):
        self._assert_tracking_evidence_is_forwarded(
            "D01",
            "Track AB123456789IN",
            {
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
            "The latest available checkpoint is dated 2 November 2026.",
        )

    def test_D02_conflicting_status_evidence_is_preserved(self):
        self._assert_tracking_evidence_is_forwarded(
            "D02",
            "Track AB123456789IN",
            {
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
            "The tracking information contains conflicting status details.",
        )

    def test_D06_new_checkpoint_does_not_change_overall_status(self):
        self._assert_tracking_evidence_is_forwarded(
            "D06",
            "Track AB123456789IN",
            {
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
            "A new checkpoint is recorded. The overall status remains In Transit.",
        )


    def test_B01_missing_number_requests_clarification(self):
        self._assert_no_tracking_for_general_message(
            "Please track my parcel",
            "Please provide the consignment number.",
        )

    def test_B02_incomplete_number_requests_correction(self):
        self._assert_no_tracking_for_general_message(
            "Track AB12345",
            "That consignment number appears incomplete. "
            "Please check and provide the full number.",
        )

    def test_B03_invalid_number_requests_correction(self):
        self._assert_no_tracking_for_general_message(
            "Track INVALID123",
            "That does not appear to be a valid consignment number. "
            "Please check it and try again.",
        )

    def test_B04_multiple_numbers_requests_selection(self):
        self._assert_no_tracking_for_general_message(
            "Track AB123456789IN and CD987654321IN",
            "I found two possible consignment numbers. "
            "Which one would you like me to track?",
        )

    def test_B06_ambiguous_previous_shipment_requests_clarification(self):
        self._assert_no_tracking_for_general_message(
            "What happened to that parcel?",
            "Which parcel are you referring to? "
            "Please identify the consignment number.",
        )


class TestStage7Step5PythonEnforcement(unittest.TestCase):

    def test_F07_model_supplied_user_id_is_rejected(self):
        with patch.dict(
            AVAILABLE_TOOLS,
            {"track_india_post": mock_tracking_tool},
        ) as _:
            result = execute_tool(
                "track_india_post",
                {
                    "consignment_number": "AB123456789IN",
                    "user_id": 123,
                },
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Invalid tool arguments.")

    def test_F08_unknown_tool_is_rejected(self):
        result = execute_tool(
            "unknown_tool",
            {"consignment_number": "AB123456789IN"},
        )

        self.assertFalse(result["success"])
        self.assertIn("Unknown or unauthorized tool", result["error"])

    def test_F09_guest_permission_denial_prevents_execution(self):
        calls = []

        def tracking_must_not_run(consignment_number, user_id=None):
            calls.append(consignment_number)
            return {"success": True}

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

        with patch.dict(
            AVAILABLE_TOOLS,
            {"track_india_post": tracking_must_not_run},
        ):
            result = execute_tool(
                "track_india_post",
                {"consignment_number": "AB123456789IN"},
                assistant_config=restricted_config,
            )

        self.assertFalse(result["success"])
        self.assertEqual(
            result["error"],
            "Tool is not permitted for this user.",
        )
        self.assertEqual(calls, [])

    def test_F10_registered_user_permission_denial_prevents_execution(self):
        calls = []

        def tracking_must_not_run(consignment_number, user_id=None):
            calls.append(consignment_number)
            return {"success": True}

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

        with patch.dict(
            AVAILABLE_TOOLS,
            {"track_india_post": tracking_must_not_run},
        ):
            result = execute_tool(
                "track_india_post",
                {"consignment_number": "AB123456789IN"},
                user_context={"user_id": 123},
                assistant_config=restricted_config,
            )

        self.assertFalse(result["success"])
        self.assertEqual(
            result["error"],
            "Tool is not permitted for this user.",
        )
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
