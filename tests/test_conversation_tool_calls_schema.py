
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path


class TestConversationToolCallsSchema(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.test_db_path = (
            Path(cls.temp_dir.name) / "test_conversation_tool_calls.db"
        )

        os.environ["INDIAPOST_DB_PATH"] = str(cls.test_db_path)

        from database.schema import (
            create_users_table,
            create_conversations_table,
            create_conversation_messages_table,
            create_conversation_requests_table,
            create_conversation_tool_rounds_table,
            create_conversation_tool_calls_table,
        )

        cls.create_users_table = create_users_table
        cls.create_conversations_table = create_conversations_table
        cls.create_conversation_messages_table = (
            create_conversation_messages_table
        )
        cls.create_conversation_requests_table = (
            create_conversation_requests_table
        )
        cls.create_conversation_tool_rounds_table = (
            create_conversation_tool_rounds_table
        )
        cls.create_conversation_tool_calls_table = (
            create_conversation_tool_calls_table
        )

        cls.create_users_table()
        cls.create_conversations_table()
        cls.create_conversation_messages_table()
        cls.create_conversation_requests_table()
        cls.create_conversation_tool_rounds_table()
        cls.create_conversation_tool_calls_table()

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()
        os.environ.pop("INDIAPOST_DB_PATH", None)

    def setUp(self):
        self.connection = sqlite3.connect(self.test_db_path)
        self.connection.execute("PRAGMA foreign_keys = ON")

        self.connection.execute("""
            DELETE FROM conversation_tool_calls
        """)

        self.connection.execute("""
            DELETE FROM conversation_tool_rounds
        """)

        self.connection.execute("""
            DELETE FROM conversation_requests
        """)

        self.connection.execute("""
            DELETE FROM conversations
            WHERE conversation_id = 'another-conversation'
        """)

        self.connection.execute("""
            DELETE FROM conversation_messages
            WHERE conversation_id = 'conversation-tool-test'
              AND sequence_number > 1
        """)

        self.connection.commit()

        self.connection.execute("""
            INSERT OR IGNORE INTO users (
                user_id,
                external_user_id,
                created_at
            )
            VALUES (1, 'tool-schema-test-user', '2026-01-01T00:00:00')
        """)

        self.connection.execute("""
            INSERT OR IGNORE INTO conversations (
                conversation_id,
                user_id,
                channel,
                channel_conversation_id,
                status,
                provider,
                created_at,
                updated_at
            )
            VALUES (
                'conversation-tool-test',
                1,
                'test',
                'tool-schema-channel',
                'active',
                'openai',
                '2026-01-01T00:00:00',
                '2026-01-01T00:00:00'
            )
        """)

        self.connection.execute("""
            INSERT OR IGNORE INTO conversation_messages (
                conversation_id,
                sequence_number,
                role,
                content,
                created_at
            )
            VALUES (
                'conversation-tool-test',
                1,
                'assistant',
                'Function call',
                '2026-01-01T00:00:00'
            )
        """)

        self.call_message_id = self.connection.execute("""
            SELECT message_id
            FROM conversation_messages
            WHERE conversation_id = 'conversation-tool-test'
              AND sequence_number = 1
        """).fetchone()[0]

        self.connection.commit()

    def tearDown(self):
        self.connection.close()

    def insert_request(
        self,
        request_id="request-tool-001",
        conversation_id="conversation-tool-test",
        user_message_id=None,
    ):
        if user_message_id is None:
            user_message_id = self.connection.execute("""
                SELECT message_id
                FROM conversation_messages
                WHERE conversation_id = ?
                  AND sequence_number = 1
            """, (conversation_id,)).fetchone()

            if user_message_id is None:
                raise ValueError("A user message is required.")

            user_message_id = user_message_id[0]

        self.connection.execute("""
            INSERT INTO conversation_requests (
                request_id,
                conversation_id,
                user_message_id,
                request_hash,
                status,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, 'processing', ?, ?)
        """, (
            request_id,
            conversation_id,
            user_message_id,
            "test-hash",
            "2026-01-01T00:00:00",
            "2026-01-01T00:00:00",
        ))

        self.connection.commit()

    def insert_tool_call(
        self,
        tool_execution_id="execution-001",
        conversation_id="conversation-tool-test",
        request_id=None,
        round_number=1,
        tool_call_id="call-001",
        tool_name="track_shipment",
        arguments='{"tracking_number": "AB123456789IN"}',
        call_message_id=None,
        output_message_id=None,
        status="pending",
    ):
        if call_message_id is None:
            call_message_id = self.call_message_id

        self.connection.execute("""
            INSERT INTO conversation_tool_calls (
                tool_execution_id,
                conversation_id,
                request_id,
                round_number,
                tool_call_id,
                tool_name,
                arguments,
                call_message_id,
                output_message_id,
                status,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            tool_execution_id,
            conversation_id,
            request_id,
            round_number,
            tool_call_id,
            tool_name,
            arguments,
            call_message_id,
            output_message_id,
            status,
            "2026-01-01T00:00:00",
            "2026-01-01T00:00:00",
        ))

        self.connection.commit()

    def test_tool_calls_table_exists(self):
        result = self.connection.execute("""
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'conversation_tool_calls'
        """).fetchone()

        self.assertIsNotNone(result)

    def test_tool_calls_table_columns(self):
        columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info(conversation_tool_calls)"
            )
        }

        expected = {
            "tool_execution_id",
            "conversation_id",
            "request_id",
            "round_id",
            "round_number",
            "tool_call_id",
            "tool_name",
            "arguments",
            "call_message_id",
            "output_message_id",
            "status",
            "error_code",
            "created_at",
            "started_at",
            "updated_at",
            "completed_at",
        }

        self.assertEqual(columns, expected)

    def test_valid_tool_call_can_be_inserted(self):
        self.insert_tool_call()

        result = self.connection.execute("""
            SELECT tool_call_id, status
            FROM conversation_tool_calls
            WHERE tool_execution_id = 'execution-001'
        """).fetchone()

        self.assertEqual(result, ("call-001", "pending"))

    def test_duplicate_tool_call_id_in_same_conversation_is_rejected(self):
        self.insert_tool_call()

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_tool_call(
                tool_execution_id="execution-002"
            )

    def test_invalid_status_is_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_tool_call(status="unknown")

    def test_zero_round_number_is_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_tool_call(round_number=0)

    def test_nonexistent_conversation_is_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_tool_call(
                conversation_id="missing-conversation"
            )

    def test_request_must_belong_to_same_conversation(self):
        self.connection.execute("""
            INSERT INTO conversations (
                conversation_id,
                user_id,
                channel,
                channel_conversation_id,
                status,
                provider,
                created_at,
                updated_at
            )
            VALUES (
                'another-conversation',
                1,
                'test',
                'another-tool-channel',
                'active',
                'openai',
                '2026-01-01T00:00:00',
                '2026-01-01T00:00:00'
            )
        """)

        self.connection.execute("""
            INSERT INTO conversation_messages (
                conversation_id,
                sequence_number,
                role,
                content,
                created_at
            )
            VALUES (
                'another-conversation',
                1,
                'user',
                'Another request',
                '2026-01-01T00:00:00'
            )
        """)

        self.connection.commit()

        self.insert_request(
            request_id="request-another",
            conversation_id="another-conversation",
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_tool_call(
                request_id="request-another"
            )

    def test_call_message_must_belong_to_same_conversation(self):
        self.connection.execute("""
            INSERT INTO conversations (
                conversation_id,
                user_id,
                channel,
                channel_conversation_id,
                status,
                provider,
                created_at,
                updated_at
            )
            VALUES (
                'another-conversation',
                1,
                'test',
                'another-tool-channel',
                'active',
                'openai',
                '2026-01-01T00:00:00',
                '2026-01-01T00:00:00'
            )
        """)

        self.connection.execute("""
            INSERT INTO conversation_messages (
                conversation_id,
                sequence_number,
                role,
                content,
                created_at
            )
            VALUES (
                'another-conversation',
                1,
                'assistant',
                'Another function call',
                '2026-01-01T00:00:00'
            )
        """)

        self.connection.commit()

        another_message_id = self.connection.execute("""
            SELECT message_id
            FROM conversation_messages
            WHERE conversation_id = 'another-conversation'
        """).fetchone()[0]

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_tool_call(
                call_message_id=another_message_id
            )

    def test_output_message_must_belong_to_same_conversation(self):
        self.connection.execute("""
            INSERT INTO conversations (
                conversation_id,
                user_id,
                channel,
                channel_conversation_id,
                status,
                provider,
                created_at,
                updated_at
            )
            VALUES (
                'another-conversation',
                1,
                'test',
                'another-tool-channel',
                'active',
                'openai',
                '2026-01-01T00:00:00',
                '2026-01-01T00:00:00'
            )
        """)

        self.connection.execute("""
            INSERT INTO conversation_messages (
                conversation_id,
                sequence_number,
                role,
                content,
                created_at
            )
            VALUES (
                'another-conversation',
                1,
                'tool',
                'Tool output',
                '2026-01-01T00:00:00'
            )
        """)

        self.connection.commit()

        another_message_id = self.connection.execute("""
            SELECT message_id
            FROM conversation_messages
            WHERE conversation_id = 'another-conversation'
        """).fetchone()[0]

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_tool_call(
                output_message_id=another_message_id,
                status="completed",
            )

    def test_null_request_id_is_allowed(self):
        self.insert_tool_call(request_id=None)

        result = self.connection.execute("""
            SELECT request_id
            FROM conversation_tool_calls
            WHERE tool_execution_id = 'execution-001'
        """).fetchone()

        self.assertEqual(result, (None,))

    def test_null_output_message_is_allowed(self):
        self.insert_tool_call(output_message_id=None)

        result = self.connection.execute("""
            SELECT output_message_id
            FROM conversation_tool_calls
            WHERE tool_execution_id = 'execution-001'
        """).fetchone()

        self.assertEqual(result, (None,))

    def test_valid_request_can_be_linked(self):
        self.insert_request()

        self.insert_tool_call(
            request_id="request-tool-001"
        )

        result = self.connection.execute("""
            SELECT request_id
            FROM conversation_tool_calls
            WHERE tool_execution_id = 'execution-001'
        """).fetchone()

        self.assertEqual(result, ("request-tool-001",))

    def test_conversation_deletion_cascades_to_tool_calls(self):
        self.insert_tool_call()

        self.connection.execute("""
            DELETE FROM conversations
            WHERE conversation_id = 'conversation-tool-test'
        """)

        self.connection.commit()

        count = self.connection.execute("""
            SELECT COUNT(*)
            FROM conversation_tool_calls
        """).fetchone()[0]

        self.assertEqual(count, 0)

    def test_integrity_checks(self):
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
