
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from database.schema import (
    create_users_table,
    create_conversations_table,
    create_conversation_messages_table,
    create_conversation_requests_table,
)


class TestConversationRequestRepository(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.test_db_path = (
            Path(cls.temp_dir.name) / "test_conversation_requests.db"
        )

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
            VALUES (1, 'repository-test-user', '2026-01-01T00:00:00')
        """)

        connection.commit()
        connection.close()

        create_conversations_table()
        create_conversation_messages_table()
        create_conversation_requests_table()

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
        os.environ.pop("INDIAPOST_DB_PATH", None)

    def setUp(self):
        self.connection = sqlite3.connect(self.test_db_path)
        self.connection.execute("PRAGMA foreign_keys = ON")

        # Reset request and message data for independent tests.
        self.connection.execute("DELETE FROM conversation_requests")
        self.connection.execute("DELETE FROM conversation_messages")

        # Reset conversations while preserving the two test conversations.
        self.connection.execute("""
            UPDATE conversations
            SET
                status = 'active',
                closed_at = NULL
        """)

        self.connection.commit()

    def tearDown(self):
        self.connection.close()

    def create_test_user(self):
        self.connection.execute("""
            INSERT OR IGNORE INTO users (
                user_id,
                external_user_id,
                created_at
            )
            VALUES (1, 'repository-test-user', '2026-01-01T00:00:00')
        """)
        self.connection.commit()

    def get_message_count(self, conversation_id):
        return self.connection.execute(
            """
            SELECT COUNT(*)
            FROM conversation_messages
            WHERE conversation_id = ?
            """,
            (conversation_id,)
        ).fetchone()[0]

    def get_request_row(self, request_id):
        row = self.connection.execute(
            """
            SELECT *
            FROM conversation_requests
            WHERE request_id = ?
            """,
            (request_id,)
        ).fetchone()

        return row

    def test_create_request_stores_request_and_user_message(self):
        from database.conversation_request_repository import create_request

        result = create_request(
            request_id="request-create-001",
            conversation_id=self.registered_conversation_id,
            user_message="Track my parcel",
            user_id=1,
        )

        self.assertTrue(result["created"])
        self.assertEqual(result["status"], "processing")
        self.assertEqual(result["request_id"], "request-create-001")
        self.assertIsNotNone(result["user_message_id"])

        self.assertEqual(
            self.get_message_count(self.registered_conversation_id),
            1,
        )

        message = self.connection.execute("""
            SELECT role, content
            FROM conversation_messages
            WHERE message_id = ?
        """, (result["user_message_id"],)).fetchone()

        self.assertEqual(message, ("user", "Track my parcel"))

    def test_duplicate_request_does_not_create_another_message(self):
        from database.conversation_request_repository import create_request

        first = create_request(
            request_id="request-duplicate-001",
            conversation_id=self.registered_conversation_id,
            user_message="Track my parcel",
            user_id=1,
        )

        second = create_request(
            request_id="request-duplicate-001",
            conversation_id=self.registered_conversation_id,
            user_message="Track my parcel",
            user_id=1,
        )

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(
            first["user_message_id"],
            second["user_message_id"],
        )
        self.assertEqual(
            self.get_message_count(self.registered_conversation_id),
            1,
        )

    def test_reused_request_id_with_different_message_is_rejected(self):
        from database.conversation_request_repository import create_request

        create_request(
            request_id="request-mismatch-001",
            conversation_id=self.registered_conversation_id,
            user_message="Track my parcel",
            user_id=1,
        )

        with self.assertRaises(ValueError):
            create_request(
                request_id="request-mismatch-001",
                conversation_id=self.registered_conversation_id,
                user_message="Cancel my parcel",
                user_id=1,
            )

        self.assertEqual(
            self.get_message_count(self.registered_conversation_id),
            1,
        )

    def test_request_id_cannot_be_reused_in_another_conversation(self):
        from database.conversation_request_repository import create_request

        create_request(
            request_id="request-cross-conversation-001",
            conversation_id=self.registered_conversation_id,
            user_message="Track my parcel",
            user_id=1,
        )

        with self.assertRaises(ValueError):
            create_request(
                request_id="request-cross-conversation-001",
                conversation_id=self.guest_conversation_id,
                user_message="Track my parcel",
            )

        self.assertEqual(
            self.get_message_count(self.guest_conversation_id),
            0,
        )

    def test_guest_request_can_be_created(self):
        from database.conversation_request_repository import create_request

        result = create_request(
            request_id="request-guest-001",
            conversation_id=self.guest_conversation_id,
            user_message="Where is my parcel?",
        )

        self.assertTrue(result["created"])
        self.assertEqual(result["status"], "processing")

    def test_wrong_user_cannot_create_request(self):
        from database.conversation_request_repository import create_request

        with self.assertRaises(ValueError):
            create_request(
                request_id="request-wrong-user-001",
                conversation_id=self.registered_conversation_id,
                user_message="Track my parcel",
                user_id=999,
            )

        self.assertEqual(
            self.get_message_count(self.registered_conversation_id),
            0,
        )

    def test_closed_conversation_rejects_new_request(self):
        from database.conversation_repository import close_conversation
        from database.conversation_request_repository import create_request

        self.assertTrue(
            close_conversation(
                self.registered_conversation_id,
                user_id=1,
            )
        )

        with self.assertRaises(ValueError):
            create_request(
                request_id="request-closed-001",
                conversation_id=self.registered_conversation_id,
                user_message="Track my parcel",
                user_id=1,
            )

        self.assertEqual(
            self.get_message_count(self.registered_conversation_id),
            0,
        )


    def test_get_request_returns_existing_request(self):
        from database.conversation_request_repository import (
            create_request,
            get_request,
        )

        create_request(
            request_id="request-get-001",
            conversation_id=self.registered_conversation_id,
            user_message="Track my parcel",
            user_id=1,
        )

        result = get_request("request-get-001", user_id=1)

        self.assertIsNotNone(result)
        self.assertEqual(result["request_id"], "request-get-001")
        self.assertEqual(result["status"], "processing")

    def test_get_request_returns_none_for_missing_request(self):
        from database.conversation_request_repository import get_request

        result = get_request("request-does-not-exist", user_id=1)

        self.assertIsNone(result)

    def test_wrong_user_cannot_retrieve_request(self):
        from database.conversation_request_repository import (
            create_request,
            get_request,
        )

        create_request(
            request_id="request-private-001",
            conversation_id=self.registered_conversation_id,
            user_message="Track my parcel",
            user_id=1,
        )

        result = get_request("request-private-001", user_id=999)

        self.assertIsNone(result)

    def test_guest_cannot_retrieve_registered_user_request(self):
        from database.conversation_request_repository import (
            create_request,
            get_request,
        )

        create_request(
            request_id="request-guest-access-001",
            conversation_id=self.registered_conversation_id,
            user_message="Track my parcel",
            user_id=1,
        )

        result = get_request("request-guest-access-001")

        self.assertIsNone(result)

    def test_complete_request_links_assistant_response(self):
        from database.conversation_request_repository import (
            create_request,
            complete_request,
        )

        request = create_request(
            request_id="request-complete-001",
            conversation_id=self.registered_conversation_id,
            user_message="Track my parcel",
            user_id=1,
        )

        self.connection.execute("""
            INSERT INTO conversation_messages (
                conversation_id,
                sequence_number,
                role,
                content,
                created_at
            )
            VALUES (?, 2, 'assistant', ?, '2026-01-01T00:01:00')
        """, (
            self.registered_conversation_id,
            "Your parcel is in transit.",
        ))

        self.connection.commit()

        response_id = self.connection.execute("""
            SELECT message_id
            FROM conversation_messages
            WHERE conversation_id = ?
              AND sequence_number = 2
        """, (self.registered_conversation_id,)).fetchone()[0]

        result = complete_request(
            request_id="request-complete-001",
            response_message_id=response_id,
            user_id=1,
        )

        self.assertTrue(result)

        stored = self.get_request_row("request-complete-001")

        self.assertEqual(stored[3], response_id)
        self.assertEqual(stored[5], "completed")
        self.assertIsNotNone(stored[9])

    def test_completing_same_request_with_same_response_is_idempotent(self):
        from database.conversation_request_repository import (
            create_request,
            complete_request,
        )

        create_request(
            request_id="request-complete-repeat-001",
            conversation_id=self.registered_conversation_id,
            user_message="Track my parcel",
            user_id=1,
        )

        self.connection.execute("""
            INSERT INTO conversation_messages (
                conversation_id,
                sequence_number,
                role,
                content,
                created_at
            )
            VALUES (?, 2, 'assistant', 'Parcel is in transit.',
                    '2026-01-01T00:01:00')
        """, (self.registered_conversation_id,))

        self.connection.commit()

        response_id = self.connection.execute("""
            SELECT message_id
            FROM conversation_messages
            WHERE conversation_id = ?
              AND sequence_number = 2
        """, (self.registered_conversation_id,)).fetchone()[0]

        self.assertTrue(
            complete_request(
                "request-complete-repeat-001",
                response_id,
                user_id=1,
            )
        )

        self.assertTrue(
            complete_request(
                "request-complete-repeat-001",
                response_id,
                user_id=1,
            )
        )

    def test_completion_rejects_non_assistant_message(self):
        from database.conversation_request_repository import (
            create_request,
            complete_request,
        )

        request = create_request(
            request_id="request-invalid-response-001",
            conversation_id=self.registered_conversation_id,
            user_message="Track my parcel",
            user_id=1,
        )

        with self.assertRaises(ValueError):
            complete_request(
                "request-invalid-response-001",
                request["user_message_id"],
                user_id=1,
            )

        stored = self.get_request_row("request-invalid-response-001")
        self.assertEqual(stored[5], "processing")

    def test_completion_rejects_response_from_another_conversation(self):
        from database.conversation_request_repository import (
            create_request,
            complete_request,
        )

        create_request(
            request_id="request-cross-response-001",
            conversation_id=self.registered_conversation_id,
            user_message="Track my parcel",
            user_id=1,
        )

        self.connection.execute("""
            INSERT INTO conversation_messages (
                conversation_id,
                sequence_number,
                role,
                content,
                created_at
            )
            VALUES (?, 1, 'assistant', 'Another response',
                    '2026-01-01T00:01:00')
        """, (self.guest_conversation_id,))

        self.connection.commit()

        response_id = self.connection.execute("""
            SELECT message_id
            FROM conversation_messages
            WHERE conversation_id = ?
        """, (self.guest_conversation_id,)).fetchone()[0]

        with self.assertRaises(ValueError):
            complete_request(
                "request-cross-response-001",
                response_id,
                user_id=1,
            )


    def test_fail_request_changes_status(self):
        from database.conversation_request_repository import (
            create_request,
            fail_request,
        )

        create_request(
            request_id="request-fail-001",
            conversation_id=self.registered_conversation_id,
            user_message="Track my parcel",
            user_id=1,
        )

        self.assertTrue(
            fail_request(
                "request-fail-001",
                user_id=1,
                error_code="PROVIDER_ERROR",
            )
        )

        stored = self.get_request_row("request-fail-001")

        self.assertEqual(stored[5], "failed")
        self.assertEqual(stored[6], "PROVIDER_ERROR")
        self.assertIsNone(stored[3])
        self.assertIsNone(stored[9])

    def test_interrupt_request_changes_status(self):
        from database.conversation_request_repository import (
            create_request,
            interrupt_request,
        )

        create_request(
            request_id="request-interrupt-001",
            conversation_id=self.registered_conversation_id,
            user_message="Track my parcel",
            user_id=1,
        )

        self.assertTrue(
            interrupt_request(
                "request-interrupt-001",
                user_id=1,
                error_code="PROCESS_INTERRUPTED",
            )
        )

        stored = self.get_request_row("request-interrupt-001")

        self.assertEqual(stored[5], "interrupted")
        self.assertEqual(stored[6], "PROCESS_INTERRUPTED")

    def test_failed_request_cannot_be_completed(self):
        from database.conversation_request_repository import (
            create_request,
            fail_request,
            complete_request,
        )

        request = create_request(
            request_id="request-fail-complete-001",
            conversation_id=self.registered_conversation_id,
            user_message="Track my parcel",
            user_id=1,
        )

        fail_request(
            "request-fail-complete-001",
            user_id=1,
        )

        with self.assertRaises(ValueError):
            complete_request(
                "request-fail-complete-001",
                request["user_message_id"],
                user_id=1,
            )

        stored = self.get_request_row("request-fail-complete-001")
        self.assertEqual(stored[5], "failed")

    def test_completed_request_cannot_be_failed(self):
        from database.conversation_request_repository import (
            create_request,
            complete_request,
            fail_request,
        )

        create_request(
            request_id="request-complete-fail-001",
            conversation_id=self.registered_conversation_id,
            user_message="Track my parcel",
            user_id=1,
        )

        self.connection.execute("""
            INSERT INTO conversation_messages (
                conversation_id,
                sequence_number,
                role,
                content,
                created_at
            )
            VALUES (?, 2, 'assistant', 'Parcel is in transit.',
                    '2026-01-01T00:01:00')
        """, (self.registered_conversation_id,))

        self.connection.commit()

        response_id = self.connection.execute("""
            SELECT message_id
            FROM conversation_messages
            WHERE conversation_id = ?
              AND sequence_number = 2
        """, (self.registered_conversation_id,)).fetchone()[0]

        complete_request(
            "request-complete-fail-001",
            response_id,
            user_id=1,
        )

        with self.assertRaises(ValueError):
            fail_request(
                "request-complete-fail-001",
                user_id=1,
            )

        stored = self.get_request_row("request-complete-fail-001")
        self.assertEqual(stored[5], "completed")

    def test_pending_requests_include_processing_and_interrupted(self):
        from database.conversation_request_repository import (
            create_request,
            interrupt_request,
            fail_request,
            list_pending_requests,
        )

        create_request(
            "request-pending-processing-001",
            self.registered_conversation_id,
            "Track parcel one",
            user_id=1,
        )

        create_request(
            "request-pending-interrupted-001",
            self.registered_conversation_id,
            "Track parcel two",
            user_id=1,
        )

        create_request(
            "request-pending-failed-001",
            self.registered_conversation_id,
            "Track parcel three",
            user_id=1,
        )

        interrupt_request(
            "request-pending-interrupted-001",
            user_id=1,
        )

        fail_request(
            "request-pending-failed-001",
            user_id=1,
        )

        results = list_pending_requests(
            self.registered_conversation_id,
            user_id=1,
        )

        statuses = {item["status"] for item in results}
        request_ids = {item["request_id"] for item in results}

        self.assertEqual(statuses, {"processing", "interrupted"})
        self.assertIn("request-pending-processing-001", request_ids)
        self.assertIn("request-pending-interrupted-001", request_ids)
        self.assertNotIn("request-pending-failed-001", request_ids)

    def test_pending_request_list_respects_limit(self):
        from database.conversation_request_repository import (
            create_request,
            list_pending_requests,
        )

        for number in range(3):
            create_request(
                f"request-limit-{number}",
                self.registered_conversation_id,
                f"Track parcel {number}",
                user_id=1,
            )

        results = list_pending_requests(
            self.registered_conversation_id,
            user_id=1,
            limit=2,
        )

        self.assertEqual(len(results), 2)

    def test_guest_cannot_list_registered_user_pending_requests(self):
        from database.conversation_request_repository import (
            create_request,
            list_pending_requests,
        )

        create_request(
            "request-private-pending-001",
            self.registered_conversation_id,
            "Track my parcel",
            user_id=1,
        )

        results = list_pending_requests(
            self.registered_conversation_id,
        )

        self.assertEqual(results, [])


    def test_blank_request_id_is_rejected(self):
        from database.conversation_request_repository import create_request

        with self.assertRaises(ValueError):
            create_request(
                "",
                self.registered_conversation_id,
                "Track my parcel",
                user_id=1,
            )

    def test_blank_message_is_rejected(self):
        from database.conversation_request_repository import create_request

        with self.assertRaises(ValueError):
            create_request(
                "request-blank-message-001",
                self.registered_conversation_id,
                "   ",
                user_id=1,
            )

    def test_invalid_user_id_is_rejected(self):
        from database.conversation_request_repository import create_request

        with self.assertRaises(ValueError):
            create_request(
                "request-invalid-user-001",
                self.registered_conversation_id,
                "Track my parcel",
                user_id=True,
            )

    def test_failed_insert_rolls_back_user_message(self):
        from database.conversation_request_repository import create_request

        # Simulate a database failure after the user message is inserted
        # but before the request record can be created.
        self.connection.execute("""
            CREATE TRIGGER reject_test_request
            BEFORE INSERT ON conversation_requests
            BEGIN
                SELECT RAISE(ABORT, 'simulated request insert failure');
            END;
        """)
        self.connection.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            create_request(
                "request-rollback-001",
                self.registered_conversation_id,
                "Track my parcel",
                user_id=1,
            )

        self.assertEqual(
            self.get_message_count(self.registered_conversation_id),
            0,
        )

        self.assertIsNone(
            self.get_request_row("request-rollback-001")
        )

        self.connection.execute("""
            DROP TRIGGER IF EXISTS reject_test_request
        """)
        self.connection.commit()

    def test_concurrent_duplicate_requests_create_one_message(self):
        from concurrent.futures import ThreadPoolExecutor
        from database.conversation_request_repository import create_request

        def submit_request():
            return create_request(
                "request-concurrent-001",
                self.registered_conversation_id,
                "Track my parcel",
                user_id=1,
            )

        with ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(
                lambda _: submit_request(),
                range(5),
            ))

        created_count = sum(
            1 for result in results if result["created"]
        )

        self.assertEqual(created_count, 1)
        self.assertEqual(
            self.get_message_count(self.registered_conversation_id),
            1,
        )

        self.assertEqual(
            self.connection.execute("""
                SELECT COUNT(*)
                FROM conversation_requests
                WHERE request_id = 'request-concurrent-001'
            """).fetchone()[0],
            1,
        )

