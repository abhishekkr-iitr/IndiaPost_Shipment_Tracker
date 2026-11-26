
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from database.schema import (
    create_users_table,
    create_conversations_table,
    create_conversation_messages_table,
    create_conversation_requests_table,
    create_conversation_tool_rounds_table,
    create_conversation_tool_calls_table,
)


class TestConversationToolCallRepository(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.test_db_path = (
            Path(cls.temp_dir.name) / "test_conversation_tool_calls.db"
        )

        cls.previous_db_path = os.environ.get("INDIAPOST_DB_PATH")
        os.environ["INDIAPOST_DB_PATH"] = str(cls.test_db_path)

        create_users_table()

        connection = sqlite3.connect(cls.test_db_path)
        connection.execute("PRAGMA foreign_keys = ON")

        connection.execute("""
            INSERT INTO users (
                user_id,
                external_user_id,
                created_at
            )
            VALUES (1, 'tool-repository-test-user', '2026-01-01T00:00:00')
        """)

        connection.commit()
        connection.close()

        create_conversations_table()
        create_conversation_messages_table()
        create_conversation_requests_table()
        create_conversation_tool_rounds_table()
        create_conversation_tool_calls_table()

        from database.conversation_repository import create_conversation

        cls.create_conversation = create_conversation

        cls.registered_conversation_id = create_conversation(
            channel="test",
            user_id=1,
        )

        cls.guest_conversation_id = create_conversation(
            channel="test",
            user_id=None,
        )

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

        if cls.previous_db_path is None:
            os.environ.pop("INDIAPOST_DB_PATH", None)
        else:
            os.environ["INDIAPOST_DB_PATH"] = cls.previous_db_path

    def setUp(self):
        self.connection = sqlite3.connect(self.test_db_path)
        self.connection.execute("PRAGMA foreign_keys = ON")

        self.connection.execute("DELETE FROM conversation_tool_calls")
        self.connection.execute("DELETE FROM conversation_tool_rounds")
        self.connection.execute("DELETE FROM conversation_requests")
        self.connection.execute("DELETE FROM conversation_messages")

        self.connection.execute("""
            UPDATE conversations
            SET
                status = 'active',
                closed_at = NULL
        """)

        self.connection.commit()

    def tearDown(self):
        self.connection.close()

    def create_request(self, request_id="request-tool-test-001"):
        from database.conversation_request_repository import create_request

        return create_request(
            request_id=request_id,
            conversation_id=self.registered_conversation_id,
            user_message="Track my parcel",
            user_id=1,
        )

    def create_execution(
        self,
        conversation_id=None,
        request_id=None,
        user_id=1,
        tool_call_id="call-test-001",
        tool_name="track_shipment",
        arguments='{"tracking_number": "AB123456789IN"}',
        call_content='{"type": "function_call", "name": "track_shipment"}',
        round_number=1,
    ):
        from database.conversation_tool_call_repository import (
            create_tool_execution,
        )

        if conversation_id is None:
            conversation_id = self.registered_conversation_id

        return create_tool_execution(
            conversation_id=conversation_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            call_content=call_content,
            round_number=round_number,
            request_id=request_id,
            user_id=user_id,
        )

    def get_execution_row(self, tool_execution_id):
        return self.connection.execute("""
            SELECT *
            FROM conversation_tool_calls
            WHERE tool_execution_id = ?
        """, (tool_execution_id,)).fetchone()

    def get_message_count(self, conversation_id):
        return self.connection.execute("""
            SELECT COUNT(*)
            FROM conversation_messages
            WHERE conversation_id = ?
        """, (conversation_id,)).fetchone()[0]

    def test_create_execution_stores_call_message_and_pending_record(self):
        result = self.create_execution()

        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["tool_name"], "track_shipment")
        self.assertEqual(result["round_number"], 1)
        self.assertIsNotNone(result["call_message_id"])

        message = self.connection.execute("""
            SELECT role, tool_name, tool_call_id
            FROM conversation_messages
            WHERE message_id = ?
        """, (result["call_message_id"],)).fetchone()

        self.assertEqual(
            message,
            ("assistant", "track_shipment", "call-test-001")
        )

        self.assertEqual(
            self.get_message_count(self.registered_conversation_id),
            1
        )

    def test_create_execution_links_to_existing_request(self):
        request = self.create_request()

        result = self.create_execution(
            request_id=request["request_id"]
        )

        self.assertEqual(
            result["request_id"],
            request["request_id"]
        )

    def test_execution_rejects_request_from_another_conversation(self):
        request = self.create_request()

        with self.assertRaises(ValueError):
            self.create_execution(
                conversation_id=self.guest_conversation_id,
                request_id=request["request_id"],
                user_id=None,
            )

        self.assertEqual(
            self.get_message_count(self.guest_conversation_id),
            0
        )

    def test_duplicate_tool_call_id_is_rejected(self):
        self.create_execution()

        with self.assertRaises(sqlite3.IntegrityError):
            self.create_execution()

        self.assertEqual(
            self.get_message_count(self.registered_conversation_id),
            1
        )

    def test_invalid_json_arguments_are_rejected(self):
        with self.assertRaises(ValueError):
            self.create_execution(arguments="{invalid json")

        self.assertEqual(
            self.get_message_count(self.registered_conversation_id),
            0
        )

    def test_invalid_round_number_is_rejected(self):
        with self.assertRaises(ValueError):
            self.create_execution(round_number=0)

        with self.assertRaises(ValueError):
            self.create_execution(round_number=True)

    def test_closed_conversation_rejects_execution(self):
        from database.conversation_repository import close_conversation

        self.assertTrue(
            close_conversation(
                self.registered_conversation_id,
                user_id=1,
            )
        )

        with self.assertRaises(ValueError):
            self.create_execution()

        self.assertEqual(
            self.get_message_count(self.registered_conversation_id),
            0
        )

    def test_guest_execution_can_be_created(self):
        result = self.create_execution(
            conversation_id=self.guest_conversation_id,
            user_id=None,
        )

        self.assertEqual(result["status"], "pending")

    def test_wrong_user_cannot_create_execution(self):
        with self.assertRaises(ValueError):
            self.create_execution(user_id=999)

        self.assertEqual(
            self.get_message_count(self.registered_conversation_id),
            0
        )

    def test_get_execution_returns_owned_execution(self):
        from database.conversation_tool_call_repository import (
            get_tool_execution,
        )

        created = self.create_execution()

        result = get_tool_execution(
            created["tool_execution_id"],
            user_id=1,
        )

        self.assertEqual(
            result["tool_execution_id"],
            created["tool_execution_id"]
        )

    def test_wrong_user_cannot_retrieve_execution(self):
        from database.conversation_tool_call_repository import (
            get_tool_execution,
        )

        created = self.create_execution()

        result = get_tool_execution(
            created["tool_execution_id"],
            user_id=999,
        )

        self.assertIsNone(result)

    def test_guest_cannot_retrieve_registered_user_execution(self):
        from database.conversation_tool_call_repository import (
            get_tool_execution,
        )

        created = self.create_execution()

        result = get_tool_execution(
            created["tool_execution_id"]
        )

        self.assertIsNone(result)

    def test_list_executions_returns_conversation_records(self):
        from database.conversation_tool_call_repository import (
            list_tool_executions,
        )

        first = self.create_execution(
            tool_call_id="call-list-001",
            round_number=1,
        )

        second = self.create_execution(
            tool_call_id="call-list-002",
            round_number=2,
        )

        results = list_tool_executions(
            self.registered_conversation_id,
            user_id=1,
        )

        self.assertEqual(len(results), 2)
        self.assertEqual(
            [item["tool_execution_id"] for item in results],
            [
                first["tool_execution_id"],
                second["tool_execution_id"],
            ]
        )

    def test_list_executions_can_filter_by_request(self):
        from database.conversation_tool_call_repository import (
            list_tool_executions,
        )

        first_request = self.create_request("request-list-001")

        self.create_execution(
            request_id=first_request["request_id"],
            tool_call_id="call-request-001",
        )

        second_request = self.create_request("request-list-002")

        self.create_execution(
            request_id=second_request["request_id"],
            tool_call_id="call-request-002",
        )

        results = list_tool_executions(
            self.registered_conversation_id,
            user_id=1,
            request_id="request-list-001",
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0]["request_id"],
            "request-list-001"
        )

    def test_pending_execution_can_be_started(self):
        from database.conversation_tool_call_repository import (
            start_tool_execution,
        )

        created = self.create_execution()

        result = start_tool_execution(
            created["tool_execution_id"],
            user_id=1,
        )

        self.assertTrue(result)

        stored = self.get_execution_row(created["tool_execution_id"])

        self.assertEqual(stored[10], "running")
        self.assertIsNotNone(stored[13])

    def test_running_execution_cannot_be_started_again(self):
        from database.conversation_tool_call_repository import (
            start_tool_execution,
        )

        created = self.create_execution()

        start_tool_execution(
            created["tool_execution_id"],
            user_id=1,
        )

        with self.assertRaises(ValueError):
            start_tool_execution(
                created["tool_execution_id"],
                user_id=1,
            )

    def test_pending_execution_cannot_be_completed(self):
        from database.conversation_tool_call_repository import (
            complete_tool_execution,
        )

        created = self.create_execution()

        with self.assertRaises(ValueError):
            complete_tool_execution(
                created["tool_execution_id"],
                "Shipment is in transit.",
                user_id=1,
            )

        self.assertEqual(
            self.get_message_count(self.registered_conversation_id),
            1
        )

    def test_running_execution_can_be_completed_atomically(self):
        from database.conversation_tool_call_repository import (
            start_tool_execution,
            complete_tool_execution,
        )

        created = self.create_execution()

        start_tool_execution(
            created["tool_execution_id"],
            user_id=1,
        )

        result = complete_tool_execution(
            created["tool_execution_id"],
            '{"status": "In Transit"}',
            user_id=1,
        )

        self.assertTrue(result)

        stored = self.get_execution_row(created["tool_execution_id"])

        self.assertEqual(stored[10], "completed")
        self.assertIsNotNone(stored[9])
        self.assertIsNotNone(stored[15])

        output = self.connection.execute("""
            SELECT role, content, tool_call_id
            FROM conversation_messages
            WHERE message_id = ?
        """, (stored[9],)).fetchone()

        self.assertEqual(
            output,
            ("tool", '{"status": "In Transit"}', "call-test-001")
        )

    def test_completion_is_idempotent_for_identical_output(self):
        from database.conversation_tool_call_repository import (
            start_tool_execution,
            complete_tool_execution,
        )

        created = self.create_execution()

        start_tool_execution(
            created["tool_execution_id"],
            user_id=1,
        )

        self.assertTrue(
            complete_tool_execution(
                created["tool_execution_id"],
                "Shipment is in transit.",
                user_id=1,
            )
        )

        message_count = self.get_message_count(
            self.registered_conversation_id
        )

        self.assertTrue(
            complete_tool_execution(
                created["tool_execution_id"],
                "Shipment is in transit.",
                user_id=1,
            )
        )

        self.assertEqual(
            self.get_message_count(self.registered_conversation_id),
            message_count
        )

    def test_completed_execution_rejects_different_output(self):
        from database.conversation_tool_call_repository import (
            start_tool_execution,
            complete_tool_execution,
        )

        created = self.create_execution()

        start_tool_execution(
            created["tool_execution_id"],
            user_id=1,
        )

        complete_tool_execution(
            created["tool_execution_id"],
            "First output",
            user_id=1,
        )

        with self.assertRaises(ValueError):
            complete_tool_execution(
                created["tool_execution_id"],
                "Different output",
                user_id=1,
            )

    def test_failed_pending_execution_cannot_be_started(self):
        from database.conversation_tool_call_repository import (
            fail_tool_execution,
            start_tool_execution,
        )

        created = self.create_execution()

        fail_tool_execution(
            created["tool_execution_id"],
            user_id=1,
            error_code="VALIDATION_ERROR",
        )

        with self.assertRaises(ValueError):
            start_tool_execution(
                created["tool_execution_id"],
                user_id=1,
            )

        stored = self.get_execution_row(created["tool_execution_id"])
        self.assertEqual(stored[10], "failed")
        self.assertEqual(stored[11], "VALIDATION_ERROR")

    def test_running_execution_can_be_failed(self):
        from database.conversation_tool_call_repository import (
            start_tool_execution,
            fail_tool_execution,
        )

        created = self.create_execution()

        start_tool_execution(
            created["tool_execution_id"],
            user_id=1,
        )

        self.assertTrue(
            fail_tool_execution(
                created["tool_execution_id"],
                user_id=1,
                error_code="TOOL_ERROR",
            )
        )

        stored = self.get_execution_row(created["tool_execution_id"])
        self.assertEqual(stored[10], "failed")
        self.assertEqual(stored[11], "TOOL_ERROR")

    def test_running_execution_can_be_interrupted(self):
        from database.conversation_tool_call_repository import (
            start_tool_execution,
            interrupt_tool_execution,
        )

        created = self.create_execution()

        start_tool_execution(
            created["tool_execution_id"],
            user_id=1,
        )

        self.assertTrue(
            interrupt_tool_execution(
                created["tool_execution_id"],
                user_id=1,
                error_code="PROCESS_INTERRUPTED",
            )
        )

        stored = self.get_execution_row(created["tool_execution_id"])
        self.assertEqual(stored[10], "interrupted")
        self.assertEqual(stored[11], "PROCESS_INTERRUPTED")

    def test_pending_execution_cannot_be_interrupted(self):
        from database.conversation_tool_call_repository import (
            interrupt_tool_execution,
        )

        created = self.create_execution()

        with self.assertRaises(ValueError):
            interrupt_tool_execution(
                created["tool_execution_id"],
                user_id=1,
            )

    def test_interrupted_execution_cannot_be_restarted(self):
        from database.conversation_tool_call_repository import (
            start_tool_execution,
            interrupt_tool_execution,
        )

        created = self.create_execution()

        start_tool_execution(
            created["tool_execution_id"],
            user_id=1,
        )

        interrupt_tool_execution(
            created["tool_execution_id"],
            user_id=1,
        )

        with self.assertRaises(ValueError):
            start_tool_execution(
                created["tool_execution_id"],
                user_id=1,
            )

    def test_completed_execution_cannot_be_failed(self):
        from database.conversation_tool_call_repository import (
            start_tool_execution,
            complete_tool_execution,
            fail_tool_execution,
        )

        created = self.create_execution()

        start_tool_execution(
            created["tool_execution_id"],
            user_id=1,
        )

        complete_tool_execution(
            created["tool_execution_id"],
            "Shipment is in transit.",
            user_id=1,
        )

        with self.assertRaises(ValueError):
            fail_tool_execution(
                created["tool_execution_id"],
                user_id=1,
            )

        stored = self.get_execution_row(created["tool_execution_id"])
        self.assertEqual(stored[10], "completed")

    def test_failed_execution_cannot_be_interrupted(self):
        from database.conversation_tool_call_repository import (
            fail_tool_execution,
            interrupt_tool_execution,
        )

        created = self.create_execution()

        fail_tool_execution(
            created["tool_execution_id"],
            user_id=1,
        )

        with self.assertRaises(ValueError):
            interrupt_tool_execution(
                created["tool_execution_id"],
                user_id=1,
            )

    def test_identical_failure_transition_is_idempotent(self):
        from database.conversation_tool_call_repository import (
            fail_tool_execution,
        )

        created = self.create_execution()

        self.assertTrue(
            fail_tool_execution(
                created["tool_execution_id"],
                user_id=1,
                error_code="VALIDATION_ERROR",
            )
        )

        self.assertTrue(
            fail_tool_execution(
                created["tool_execution_id"],
                user_id=1,
                error_code="VALIDATION_ERROR",
            )
        )

    def test_failed_execution_rejects_different_error_code(self):
        from database.conversation_tool_call_repository import (
            fail_tool_execution,
        )

        created = self.create_execution()

        fail_tool_execution(
            created["tool_execution_id"],
            user_id=1,
            error_code="VALIDATION_ERROR",
        )

        with self.assertRaises(ValueError):
            fail_tool_execution(
                created["tool_execution_id"],
                user_id=1,
                error_code="OTHER_ERROR",
            )

    def test_failed_execution_rolls_back_call_message(self):
        self.connection.execute("""
            CREATE TRIGGER reject_tool_execution
            BEFORE INSERT ON conversation_tool_calls
            BEGIN
                SELECT RAISE(ABORT, 'simulated execution insert failure');
            END;
        """)
        self.connection.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            self.create_execution()

        self.assertEqual(
            self.get_message_count(self.registered_conversation_id),
            0
        )

        self.connection.execute("""
            DROP TRIGGER IF EXISTS reject_tool_execution
        """)
        self.connection.commit()

    def test_failed_output_insert_rolls_back_completion(self):
        from database.conversation_tool_call_repository import (
            start_tool_execution,
            complete_tool_execution,
        )

        created = self.create_execution()

        start_tool_execution(
            created["tool_execution_id"],
            user_id=1,
        )

        self.connection.execute("""
            CREATE TRIGGER reject_tool_output
            BEFORE INSERT ON conversation_messages
            WHEN NEW.role = 'tool'
            BEGIN
                SELECT RAISE(ABORT, 'simulated output insert failure');
            END;
        """)
        self.connection.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            complete_tool_execution(
                created["tool_execution_id"],
                "Shipment is in transit.",
                user_id=1,
            )

        stored = self.get_execution_row(created["tool_execution_id"])
        self.assertEqual(stored[10], "running")
        self.assertIsNone(stored[11])

        self.assertEqual(
            self.get_message_count(self.registered_conversation_id),
            1
        )

        self.connection.execute("""
            DROP TRIGGER IF EXISTS reject_tool_output
        """)
        self.connection.commit()

    def test_concurrent_start_allows_only_one_success(self):
        from database.conversation_tool_call_repository import (
            start_tool_execution,
        )

        created = self.create_execution()

        def start():
            try:
                return start_tool_execution(
                    created["tool_execution_id"],
                    user_id=1,
                )
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(lambda _: start(), range(5)))

        self.assertEqual(sum(results), 1)

        stored = self.get_execution_row(created["tool_execution_id"])
        self.assertEqual(stored[10], "running")

    def test_integrity_checks(self):
        self.create_execution()

        integrity = self.connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]

        foreign_keys = self.connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

        self.assertEqual(integrity, "ok")
        self.assertEqual(foreign_keys, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
