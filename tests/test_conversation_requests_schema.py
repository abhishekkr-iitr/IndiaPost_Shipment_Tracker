
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path


class TestConversationRequestsSchema(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.test_db_path = (
            Path(cls.temp_dir.name) / "test_conversation_requests.db"
        )

        os.environ["INDIAPOST_DB_PATH"] = str(cls.test_db_path)

        from database.schema import (
            create_users_table,
            create_conversations_table,
            create_conversation_messages_table,
            create_conversation_requests_table,
        )

        cls.create_users_table = create_users_table
        cls.create_conversations_table = create_conversations_table
        cls.create_conversation_messages_table = (
            create_conversation_messages_table
        )
        cls.create_conversation_requests_table = (
            create_conversation_requests_table
        )

        cls.create_users_table()
        cls.create_conversations_table()
        cls.create_conversation_messages_table()
        cls.create_conversation_requests_table()

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()
        os.environ.pop("INDIAPOST_DB_PATH", None)

    def setUp(self):
        self.connection = sqlite3.connect(self.test_db_path)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("""
            DELETE FROM conversation_requests
        """)

        self.connection.execute("""
            DELETE FROM conversations
            WHERE conversation_id = 'another-conversation'
        """)

        self.connection.execute("""
            DELETE FROM conversation_messages
            WHERE conversation_id = 'conversation-schema-test'
              AND sequence_number > 1
        """)

        self.connection.commit()
        
        self.connection.execute("""
            INSERT OR IGNORE INTO users (
                user_id,
                external_user_id,
                created_at
            )
            VALUES (1, 'schema-test-user', '2026-01-01T00:00:00')
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
                'conversation-schema-test',
                1,
                'test',
                'schema-test-channel',
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
                'conversation-schema-test',
                1,
                'user',
                'Track my parcel',
                '2026-01-01T00:00:00'
            )
        """)

        self.user_message_id = self.connection.execute("""
            SELECT message_id
            FROM conversation_messages
            WHERE conversation_id = 'conversation-schema-test'
              AND sequence_number = 1
        """).fetchone()[0]

        self.connection.commit()

    def tearDown(self):
        self.connection.close()

    def insert_request(
        self,
        request_id="request-001",
        conversation_id="conversation-schema-test",
        user_message_id=None,
        response_message_id=None,
        request_hash="test-hash",
        status="processing",
        error_code=None,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        completed_at=None,
    ):
        if user_message_id is None:
            user_message_id = self.user_message_id

        self.connection.execute("""
            INSERT INTO conversation_requests (
                request_id,
                conversation_id,
                user_message_id,
                response_message_id,
                request_hash,
                status,
                error_code,
                created_at,
                updated_at,
                completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            request_id,
            conversation_id,
            user_message_id,
            response_message_id,
            request_hash,
            status,
            error_code,
            created_at,
            updated_at,
            completed_at,
        ))
        self.connection.commit()

    def test_request_table_exists(self):
        result = self.connection.execute("""
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'conversation_requests'
        """).fetchone()

        self.assertIsNotNone(result)

    def test_request_table_columns(self):
        columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info(conversation_requests)"
            )
        }

        expected = {
            "request_id",
            "conversation_id",
            "user_message_id",
            "response_message_id",
            "request_hash",
            "status",
            "error_code",
            "created_at",
            "updated_at",
            "completed_at",
        }

        self.assertEqual(columns, expected)

    def test_request_can_be_inserted(self):
        self.insert_request()

        result = self.connection.execute("""
            SELECT request_id, status
            FROM conversation_requests
            WHERE request_id = 'request-001'
        """).fetchone()

        self.assertEqual(result, ("request-001", "processing"))

    def test_duplicate_request_id_is_rejected(self):
        self.insert_request()

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_request()

    def test_invalid_status_is_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_request(status="unknown")

    def test_nonexistent_conversation_is_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_request(
                conversation_id="missing-conversation"
            )

    def test_user_message_must_belong_to_same_conversation(self):
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
                'another-channel',
                'active',
                'openai',
                '2026-01-01T00:00:00',
                '2026-01-01T00:00:00'
            )
        """)

        self.connection.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_request(
                conversation_id="another-conversation"
            )

    def test_response_message_must_belong_to_same_conversation(self):
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
                'another-channel',
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
                'Another response',
                '2026-01-01T00:00:00'
            )
        """)

        self.connection.commit()

        response_id = self.connection.execute("""
            SELECT message_id
            FROM conversation_messages
            WHERE conversation_id = 'another-conversation'
        """).fetchone()[0]

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_request(response_message_id=response_id)

    def test_valid_response_message_can_be_linked(self):
        self.connection.execute("""
            INSERT INTO conversation_messages (
                conversation_id,
                sequence_number,
                role,
                content,
                created_at
            )
            VALUES (
                'conversation-schema-test',
                2,
                'assistant',
                'Your parcel is in transit.',
                '2026-01-01T00:01:00'
            )
        """)

        self.connection.commit()

        response_id = self.connection.execute("""
            SELECT message_id
            FROM conversation_messages
            WHERE conversation_id = 'conversation-schema-test'
              AND sequence_number = 2
        """).fetchone()[0]

        self.insert_request(
            response_message_id=response_id,
            status="completed",
            completed_at="2026-01-01T00:01:00",
        )

        result = self.connection.execute("""
            SELECT response_message_id, status
            FROM conversation_requests
            WHERE request_id = 'request-001'
        """).fetchone()

        self.assertEqual(
            result,
            (response_id, "completed")
        )

    def test_null_response_message_is_allowed(self):
        self.insert_request(response_message_id=None)

        result = self.connection.execute("""
            SELECT response_message_id
            FROM conversation_requests
            WHERE request_id = 'request-001'
        """).fetchone()

        self.assertEqual(result, (None,))

    def test_conversation_deletion_cascades_to_requests(self):
        self.insert_request()

        self.connection.execute("""
            DELETE FROM conversations
            WHERE conversation_id = 'conversation-schema-test'
        """)

        self.connection.commit()

        count = self.connection.execute("""
            SELECT COUNT(*)
            FROM conversation_requests
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
