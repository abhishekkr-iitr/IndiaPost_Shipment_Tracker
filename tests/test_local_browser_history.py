"""Focused tests for durable, isolated local-browser conversation history."""
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api import browser_session_store as store


class BrowserSessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "history-test.db"
        self._create_schema()
        self.connection_patcher = patch.object(store, "get_db_connection", side_effect=self._connect)
        self.connection_patcher.start()
        self.token_a = hashlib.sha256(b"session-A-secret").hexdigest()
        self.token_b = hashlib.sha256(b"session-B-secret").hexdigest()

    def tearDown(self):
        self.connection_patcher.stop()
        self.temp_dir.cleanup()

    def _connect(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _create_schema(self):
        connection = sqlite3.connect(self.db_path)
        connection.executescript("""
            CREATE TABLE conversations (
                conversation_id TEXT PRIMARY KEY, user_id INTEGER, channel TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT
            );
            CREATE TABLE conversation_messages (
                message_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                sequence_number INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL
            );
            CREATE TABLE local_web_sessions (
                token_hash TEXT PRIMARY KEY, active_conversation_id TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                FOREIGN KEY(active_conversation_id) REFERENCES conversations(conversation_id) ON DELETE SET NULL
            );
            CREATE TABLE local_web_session_conversations (
                token_hash TEXT NOT NULL, conversation_id TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(token_hash, conversation_id), UNIQUE(conversation_id),
                FOREIGN KEY(token_hash) REFERENCES local_web_sessions(token_hash) ON DELETE CASCADE,
                FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE
            );
        """)
        connection.commit()
        connection.close()

    def _conversation(self, conversation_id, status="active"):
        connection = self._connect()
        now = "2026-11-19T12:00:00+00:00"
        connection.execute(
            "INSERT INTO conversations VALUES (?, NULL, 'local_web', ?, ?, ?, NULL)",
            (conversation_id, status, now, now),
        )
        connection.commit()
        connection.close()

    def test_session_and_conversation_association_survive_new_connection(self):
        self._conversation("conversation-A")
        store.create_session(self.token_a, "2999-01-01T00:00:00+00:00")
        store.add_conversation(self.token_a, "conversation-A")
        # A separate DB connection simulates a process restart.
        restored = store.get_session(self.token_a)
        self.assertEqual(restored["active_conversation_id"], "conversation-A")

    def test_sessions_cannot_list_or_open_each_others_conversations(self):
        self._conversation("conversation-A")
        self._conversation("conversation-B")
        store.create_session(self.token_a, "2999-01-01T00:00:00+00:00")
        store.create_session(self.token_b, "2999-01-01T00:00:00+00:00")
        store.add_conversation(self.token_a, "conversation-A")
        store.add_conversation(self.token_b, "conversation-B")
        self.assertTrue(store.is_owned_conversation(self.token_a, "conversation-A"))
        self.assertFalse(store.is_owned_conversation(self.token_a, "conversation-B"))
        self.assertEqual([x["conversation_id"] for x in store.list_conversations(self.token_a)], ["conversation-A"])
        self.assertIsNone(store.set_active_conversation(self.token_a, "conversation-B"))

    def test_closed_conversation_remains_in_history(self):
        self._conversation("conversation-A", status="closed")
        store.create_session(self.token_a, "2999-01-01T00:00:00+00:00")
        store.add_conversation(self.token_a, "conversation-A")
        history = store.list_conversations(self.token_a)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "closed")

    def test_expired_session_is_not_restored(self):
        store.create_session(self.token_a, "2000-01-01T00:00:00+00:00")
        self.assertIsNone(store.get_session(self.token_a))

    def test_raw_cookie_token_is_not_stored(self):
        self._conversation("conversation-A")
        raw_token = "session-A-secret"
        store.create_session(self.token_a, "2999-01-01T00:00:00+00:00")
        store.add_conversation(self.token_a, "conversation-A")
        connection = self._connect()
        stored = connection.execute("SELECT token_hash FROM local_web_sessions").fetchone()[0]
        connection.close()
        self.assertEqual(stored, hashlib.sha256(raw_token.encode()).hexdigest())
        self.assertNotEqual(stored, raw_token)


if __name__ == "__main__":
    unittest.main()
