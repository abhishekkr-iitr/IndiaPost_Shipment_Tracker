
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


# Mock OpenAI Responses API objects

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


def make_tracking_data(consignment_number):
    return {
        "tracking_number": consignment_number,
        "carrier": "india-post",
        "current_status": "In Transit",
        "status_message": "Shipment is moving through the postal network.",
        "origin": "New Delhi",
        "destination": "Bengaluru",
        "pickup_date": "2026-11-10T09:00:00+00:00",
        "delivery_date": None,
        "checkpoints": [
            {
                "date": "2026-11-10T09:00:00+00:00",
                "location": "New Delhi",
                "status": "Picked Up",
                "message": "Shipment accepted."
            },
            {
                "date": "2026-11-11T14:30:00+00:00",
                "location": "New Delhi Sorting Centre",
                "status": "In Transit",
                "message": "Shipment dispatched."
            }
        ]
    }


class TestEndToEndUserTracking(unittest.TestCase):

    def setUp(self):
        self.previous_db_path = os.environ.get(
            "INDIAPOST_DB_PATH"
        )

        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="IndiaPost_EndToEnd_Test_"
        )

        self.test_db_path = (
            Path(self.temp_dir.name) / "test_shipments.db"
        )

        os.environ["INDIAPOST_DB_PATH"] = str(
            self.test_db_path
        )

        from database.schema import (
            create_users_table,
            create_shipments_table,
            create_user_shipments_table,
            create_checkpoints_table,
            create_tracking_checks_table,
            create_notification_preferences_table,
            create_notifications_table
        )

        # Create the seven base tables used by this end-to-end test.
        create_users_table()
        create_shipments_table()
        create_user_shipments_table()
        create_checkpoints_table()
        create_tracking_checks_table()
        create_notification_preferences_table()
        create_notifications_table()

        # Bring this isolated test database up to the current application
        # schema. Migration 6 creates monitoring_jobs; Migration 7 creates
        # the durable shipment_change_events ledger.
        # apply_migrations() uses INDIAPOST_DB_PATH via get_db_connection(),
        # so this affects only the temporary database created above.
        from database.migrations import LATEST_SCHEMA_VERSION, apply_migrations

        migration_result = apply_migrations()

        self.assertEqual(
            migration_result["current_version"],
            LATEST_SCHEMA_VERSION,
            "The end-to-end test database must use the latest application schema."
        )

        # Create a synthetic registered user.
        connection = sqlite3.connect(self.test_db_path)

        try:
            cursor = connection.cursor()

            cursor.execute("""
                INSERT INTO users (
                    external_user_id,
                    created_at
                )
                VALUES (?, ?);
            """, (
                "END_TO_END_TEST_USER",
                "2026-11-13T00:00:00+00:00"
            ))

            connection.commit()
            self.user_id = cursor.lastrowid

        finally:
            connection.close()

        from ai_tools.openai_agent import ask_openai

        self.ask_openai = ask_openai

    def tearDown(self):
        self.temp_dir.cleanup()

        if self.previous_db_path is None:
            os.environ.pop(
                "INDIAPOST_DB_PATH",
                None
            )
        else:
            os.environ["INDIAPOST_DB_PATH"] = (
                self.previous_db_path
            )

    def run_mocked_agent_request(
        self,
        consignment_number,
        user_context=None
    ):
        function_call = SimpleNamespace(
            type="function_call",
            name="track_india_post",
            arguments=json.dumps({
                "consignment_number": consignment_number
            }),
            call_id="call_test_001"
        )

        client = MockOpenAIClient([
            make_response(
                "response_1",
                [function_call]
            ),
            make_response(
                "response_2",
                [],
                "Here is the shipment tracking information."
            )
        ])

        result = self.ask_openai(
            f"Track {consignment_number}",
            client=client,
            model="mock-model",
            user_context=user_context
        )

        return result

    def get_database_counts(self):
        connection = sqlite3.connect(self.test_db_path)

        try:
            cursor = connection.cursor()

            counts = {}

            for table in (
                "shipments",
                "checkpoints",
                "user_shipments",
                "tracking_checks"
            ):
                cursor.execute(
                    f"SELECT COUNT(*) FROM {table};"
                )
                counts[table] = cursor.fetchone()[0]

            return counts

        finally:
            connection.close()

    def test_registered_user_tracking_and_repeat_request(self):
        consignment_number = "AB123456789IN"

        # Mock only the external AfterShip-facing functions.
        with patch(
            "tracking.shipment_workflow.find_tracking_id",
            return_value="mock-aftership-tracking-id"
        ), patch(
            "tracking.shipment_workflow.track_shipment",
            side_effect=lambda *args, **kwargs:
                make_tracking_data(consignment_number)
        ):

            # First request: registered user.
            first_response = self.run_mocked_agent_request(
                consignment_number,
                user_context={"user_id": self.user_id}
            )

            self.assertTrue(
                first_response.startswith(
                    "The latest recorded checkpoint shows In Transit at "
                    "New Delhi Sorting Centre on 11 November 2026."
                )
            )
            self.assertIn("15 days old and may be stale", first_response)
            self.assertIn(
                "does not confirm the shipment's current location or current status",
                first_response,
            )

            first_counts = self.get_database_counts()

            self.assertEqual(first_counts["shipments"], 1)
            self.assertEqual(first_counts["checkpoints"], 2)
            self.assertEqual(first_counts["user_shipments"], 1)
            self.assertEqual(first_counts["tracking_checks"], 1)

            # Retrieve the saved user-shipment association.
            connection = sqlite3.connect(self.test_db_path)

            try:
                cursor = connection.cursor()

                cursor.execute("""
                    SELECT
                        user_shipment_id,
                        user_id,
                        shipment_id,
                        monitoring_interval_hours,
                        monitoring_enabled
                    FROM user_shipments;
                """)

                first_subscription = cursor.fetchone()

            finally:
                connection.close()

            self.assertIsNotNone(first_subscription)

            first_subscription_id = first_subscription[0]
            self.assertEqual(first_subscription[1], self.user_id)
            self.assertEqual(first_subscription[3], 6)
            self.assertEqual(first_subscription[4], 1)

            # Second request: same user and same consignment.
            second_response = self.run_mocked_agent_request(
                consignment_number,
                user_context={"user_id": self.user_id}
            )

            self.assertTrue(
                second_response.startswith(
                    "The latest recorded checkpoint shows In Transit at "
                    "New Delhi Sorting Centre on 11 November 2026."
                )
            )
            self.assertIn("15 days old and may be stale", second_response)
            self.assertIn(
                "does not confirm the shipment's current location or current status",
                second_response,
            )

            second_counts = self.get_database_counts()

            # The existing shipment and subscription must be reused.
            self.assertEqual(second_counts["shipments"], 1)
            self.assertEqual(second_counts["user_shipments"], 1)

            # Existing checkpoints must not be duplicated.
            self.assertEqual(second_counts["checkpoints"], 2)

            # Both tracking attempts should be recorded.
            self.assertEqual(second_counts["tracking_checks"], 2)

            connection = sqlite3.connect(self.test_db_path)

            try:
                cursor = connection.cursor()

                cursor.execute("""
                    SELECT user_shipment_id
                    FROM user_shipments
                    WHERE user_id = ?;
                """, (self.user_id,))

                second_subscription_id = cursor.fetchone()[0]

            finally:
                connection.close()

            self.assertEqual(
                first_subscription_id,
                second_subscription_id
            )

    def test_guest_tracking_does_not_create_subscription(self):
        consignment_number = "CD123456789IN"

        with patch(
            "tracking.shipment_workflow.find_tracking_id",
            return_value="mock-guest-aftership-id"
        ), patch(
            "tracking.shipment_workflow.track_shipment",
            side_effect=lambda *args, **kwargs:
                make_tracking_data(consignment_number)
        ):

            response = self.run_mocked_agent_request(
                consignment_number,
                user_context=None
            )

            self.assertTrue(
                response.startswith(
                    "The latest recorded checkpoint shows In Transit at "
                    "New Delhi Sorting Centre on 11 November 2026."
                )
            )
            self.assertIn("15 days old and may be stale", response)
            self.assertIn(
                "does not confirm the shipment's current location or current status",
                response,
            )

            counts = self.get_database_counts()

            self.assertEqual(counts["shipments"], 1)
            self.assertEqual(counts["checkpoints"], 2)
            self.assertEqual(counts["user_shipments"], 0)
            self.assertEqual(counts["tracking_checks"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
