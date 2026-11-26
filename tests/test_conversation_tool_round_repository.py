import os
import tempfile
import unittest
from uuid import uuid4

from database.db_connection import get_db_connection
from database.schema import (
    create_users_table,
    create_conversations_table,
    create_conversation_messages_table,
    create_conversation_requests_table,
    create_conversation_tool_rounds_table,
    create_conversation_tool_calls_table,
)
from database.conversation_tool_round_repository import (
    create_tool_round,
    get_tool_round,
    list_tool_rounds,
    start_tool_round,
    complete_tool_round,
    set_tool_round_status,
    get_request_recovery_status,
    fail_pending_tool_round,
    interrupt_running_tool_round,
)
from database.conversation_tool_call_repository import start_tool_execution


class ConversationToolRoundRepositoryTests(unittest.TestCase):

    def setUp(self):
        self.previous_db_path = os.environ.get("INDIAPOST_DB_PATH")
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "rounds_test.db")
        os.environ["INDIAPOST_DB_PATH"] = self.db_path

        create_users_table()
        create_conversations_table()
        create_conversation_messages_table()
        create_conversation_requests_table()
        create_conversation_tool_rounds_table()
        create_conversation_tool_calls_table()

        self.conversation_id = str(uuid4())
        self.request_id = str(uuid4())

        connection = get_db_connection()
        try:
            cursor = connection.cursor()
            now = "2026-11-13T12:00:00+05:30"

            cursor.execute(
                """
                INSERT INTO conversations (
                    conversation_id, user_id, channel, channel_conversation_id,
                    status, provider, provider_conversation_id,
                    created_at, updated_at, closed_at
                )
                VALUES (?, NULL, 'test', NULL, 'active', NULL, NULL, ?, ?, NULL);
                """,
                (self.conversation_id, now, now),
            )

            cursor.execute(
                """
                INSERT INTO conversation_messages (
                    conversation_id, sequence_number, role, content,
                    tool_name, tool_call_id, provider_message_id, created_at
                )
                VALUES (?, 1, 'user', 'Track this shipment', NULL, NULL, NULL, ?);
                """,
                (self.conversation_id, now),
            )
            user_message_id = cursor.lastrowid

            cursor.execute(
                """
                INSERT INTO conversation_requests (
                    request_id, conversation_id, user_message_id,
                    response_message_id, request_hash, status, error_code,
                    created_at, updated_at, completed_at
                )
                VALUES (?, ?, ?, NULL, 'test-hash', 'processing', NULL, ?, ?, NULL);
                """,
                (
                    self.request_id,
                    self.conversation_id,
                    user_message_id,
                    now,
                    now,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def tearDown(self):
        if self.previous_db_path is None:
            os.environ.pop("INDIAPOST_DB_PATH", None)
        else:
            os.environ["INDIAPOST_DB_PATH"] = self.previous_db_path
        self.temp_dir.cleanup()

    def _calls(self):
        return [
            {
                "tool_call_id": "call_1",
                "tool_name": "track_shipment",
                "arguments": '{"tracking_number":"AB123"}',
                "call_content": '{"tracking_number":"AB123"}',
            },
            {
                "tool_call_id": "call_2",
                "tool_name": "get_tracking_history",
                "arguments": '{"tracking_number":"AB123"}',
                "call_content": '{"tracking_number":"AB123"}',
            },
        ]

    def test_create_complete_round_atomically(self):
        result = create_tool_round(
            self.conversation_id,
            1,
            self._calls(),
            request_id=self.request_id,
        )

        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["expected_call_count"], 2)
        self.assertEqual(len(result["executions"]), 2)
        self.assertTrue(all(
            item["status"] == "pending" for item in result["executions"]
        ))

        stored = get_tool_round(result["round_id"])
        self.assertEqual(stored["status"], "pending")
        self.assertEqual(stored["expected_call_count"], 2)
        self.assertEqual(len(stored["executions"]), 2)
        self.assertTrue(all(
            item["round_id"] == result["round_id"]
            for item in stored["executions"]
        ))

        connection = get_db_connection()
        try:
            count = connection.execute(
                """
                SELECT COUNT(*)
                FROM conversation_messages
                WHERE conversation_id = ? AND role = 'assistant';
                """,
                (self.conversation_id,),
            ).fetchone()[0]
            self.assertEqual(count, 2)
        finally:
            connection.close()

    def test_round_listing_filters_by_request(self):
        created = create_tool_round(
            self.conversation_id, 1, self._calls(),
            request_id=self.request_id,
        )
        self.assertEqual(
            [item["round_id"] for item in list_tool_rounds(
                self.conversation_id, request_id=self.request_id
            )],
            [created["round_id"]],
        )

    def test_wrong_guest_ownership_cannot_read_round(self):
        created = create_tool_round(
            self.conversation_id, 1, self._calls(),
            request_id=self.request_id,
        )
        self.assertIsNone(get_tool_round(created["round_id"], user_id=123))
        self.assertEqual(
            list_tool_rounds(self.conversation_id, user_id=123), []
        )

    def test_start_and_complete_round(self):
        created = create_tool_round(
            self.conversation_id, 1, self._calls(),
            request_id=self.request_id,
        )
        self.assertTrue(start_tool_round(created["round_id"]))

        connection = get_db_connection()
        try:
            connection.execute(
                """
                UPDATE conversation_tool_calls
                SET status = 'completed'
                WHERE round_id = ?;
                """,
                (created["round_id"],),
            )
            connection.commit()
        finally:
            connection.close()

        self.assertTrue(complete_tool_round(created["round_id"]))
        self.assertEqual(
            get_tool_round(created["round_id"])["status"], "completed"
        )

    def test_round_cannot_complete_until_all_calls_complete(self):
        created = create_tool_round(
            self.conversation_id, 1, self._calls(),
            request_id=self.request_id,
        )
        start_tool_round(created["round_id"])
        with self.assertRaises(ValueError):
            complete_tool_round(created["round_id"])

    def test_failed_and_interrupted_are_explicit_transitions(self):
        failed = create_tool_round(
            self.conversation_id, 1, self._calls(),
            request_id=self.request_id,
        )
        self.assertTrue(set_tool_round_status(
            failed["round_id"], "failed", error_code="known_failure"
        ))

        second_round_calls = self._calls()
        for call in second_round_calls:
            call["tool_call_id"] += "_round2"

        interrupted = create_tool_round(
            self.conversation_id, 2, second_round_calls,
            request_id=self.request_id,
        )
        start_tool_round(interrupted["round_id"])
        self.assertTrue(set_tool_round_status(
            interrupted["round_id"], "interrupted",
            error_code="uncertain_outcome",
        ))

        self.assertEqual(get_tool_round(failed["round_id"])["status"], "failed")
        self.assertEqual(
            get_tool_round(interrupted["round_id"])["status"], "interrupted"
        )

    def test_request_recovery_snapshot_is_read_only(self):
        created = create_tool_round(
            self.conversation_id, 1, self._calls(),
            request_id=self.request_id,
        )
        before = get_request_recovery_status(self.request_id)
        self.assertEqual(
            before["recommendation"],
            "pending_round_requires_controlled_execution",
        )
        self.assertEqual(before["rounds"][0]["round_id"], created["round_id"])
        self.assertEqual(
            get_tool_round(created["round_id"])["status"], "pending"
        )

    def test_request_recovery_detects_uncertain_execution(self):
        created = create_tool_round(
            self.conversation_id, 1, self._calls(),
            request_id=self.request_id,
        )
        start_tool_round(created["round_id"])

        connection = get_db_connection()
        try:
            connection.execute(
                """
                UPDATE conversation_tool_calls
                SET status = 'interrupted'
                WHERE round_id = ?;
                """,
                (created["round_id"],),
            )
            connection.execute(
                """
                UPDATE conversation_tool_rounds
                SET status = 'interrupted'
                WHERE round_id = ?;
                """,
                (created["round_id"],),
            )
            connection.commit()
        finally:
            connection.close()

        snapshot = get_request_recovery_status(self.request_id)
        self.assertEqual(
            snapshot["recommendation"],
            "manual_review_uncertain_outcome",
        )

    def test_invalid_call_does_not_create_partial_round(self):
        calls = self._calls()
        calls[1]["arguments"] = "not-json"

        with self.assertRaises(ValueError):
            create_tool_round(
                self.conversation_id, 1, calls,
                request_id=self.request_id,
            )

        self.assertEqual(list_tool_rounds(self.conversation_id), [])

    def test_duplicate_round_number_is_rejected(self):
        create_tool_round(
            self.conversation_id, 1, self._calls(),
            request_id=self.request_id,
        )
        with self.assertRaises(Exception):
            create_tool_round(
                self.conversation_id, 1, self._calls(),
                request_id=self.request_id,
            )


    def test_fail_pending_round_is_explicit_and_atomic(self):
        created = create_tool_round(
            self.conversation_id, 1, self._calls(),
            request_id=self.request_id,
        )

        self.assertTrue(
            fail_pending_tool_round(
                created["round_id"],
                error_code="operator_confirmed_abandon",
            )
        )

        stored_round = get_tool_round(created["round_id"])
        self.assertEqual(stored_round["status"], "failed")
        self.assertTrue(
            all(item["status"] == "failed" for item in stored_round["executions"])
        )

        request = get_request_recovery_status(self.request_id)
        self.assertEqual(request["request"]["status"], "failed")
        self.assertEqual(
            request["recommendation"],
            "manual_review_failed_execution",
        )

        # Repeating the same safe action is idempotent.
        self.assertTrue(fail_pending_tool_round(created["round_id"]))

    def test_fail_pending_round_rejects_started_round(self):
        created = create_tool_round(
            self.conversation_id, 1, self._calls(),
            request_id=self.request_id,
        )
        start_tool_round(created["round_id"])

        with self.assertRaises(ValueError):
            fail_pending_tool_round(created["round_id"])

        self.assertEqual(
            get_tool_round(created["round_id"])["status"],
            "running",
        )
        self.assertEqual(
            get_request_recovery_status(self.request_id)["request"]["status"],
            "processing",
        )

    def test_interrupt_running_round_marks_uncertain_and_unstarted_calls(self):
        created = create_tool_round(
            self.conversation_id, 1, self._calls(),
            request_id=self.request_id,
        )
        start_tool_round(created["round_id"])

        running_execution_id = created["executions"][0]["tool_execution_id"]
        start_tool_execution(running_execution_id)

        self.assertTrue(
            interrupt_running_tool_round(
                created["round_id"],
                error_code="operator_confirmed_interruption",
            )
        )

        stored_round = get_tool_round(created["round_id"])
        self.assertEqual(stored_round["status"], "interrupted")

        statuses = {
            item["tool_execution_id"]: item["status"]
            for item in stored_round["executions"]
        }
        self.assertEqual(statuses[running_execution_id], "interrupted")

        pending_execution_id = next(
            item["tool_execution_id"]
            for item in created["executions"]
            if item["tool_execution_id"] != running_execution_id
        )
        self.assertEqual(statuses[pending_execution_id], "failed")

        request = get_request_recovery_status(self.request_id)
        self.assertEqual(request["request"]["status"], "interrupted")
        self.assertEqual(
            request["recommendation"],
            "manual_review_uncertain_outcome",
        )


if __name__ == "__main__":
    unittest.main()
