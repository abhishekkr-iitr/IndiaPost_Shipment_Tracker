"""SQLite persistence for opaque local-browser sessions and their conversations.

Only SHA-256 digests of browser cookie tokens are stored. Conversation ownership
is additionally constrained to guest local_web conversations in every query.
"""
from __future__ import annotations

from datetime import datetime, timezone

from database.db_connection import get_db_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_session(token_hash: str, expires_at: str) -> None:
    connection = get_db_connection()
    try:
        connection.execute(
            "INSERT INTO local_web_sessions (token_hash, active_conversation_id, created_at, updated_at, expires_at) VALUES (?, NULL, ?, ?, ?)",
            (token_hash, _now(), _now(), expires_at),
        )
        connection.commit()
    finally:
        connection.close()


def get_session(token_hash: str) -> dict | None:
    connection = get_db_connection()
    try:
        row = connection.execute(
            "SELECT token_hash, active_conversation_id, expires_at FROM local_web_sessions WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is None:
            return None
        if row[2] <= _now():
            connection.execute("DELETE FROM local_web_sessions WHERE token_hash = ?", (token_hash,))
            connection.commit()
            return None
        connection.execute("UPDATE local_web_sessions SET updated_at = ? WHERE token_hash = ?", (_now(), token_hash))
        connection.commit()
        return {"token_hash": row[0], "active_conversation_id": row[1], "expires_at": row[2]}
    finally:
        connection.close()


def add_conversation(token_hash: str, conversation_id: str, make_active: bool = True) -> None:
    connection = get_db_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        session = connection.execute(
            "SELECT token_hash FROM local_web_sessions WHERE token_hash = ? AND expires_at > ?",
            (token_hash, _now()),
        ).fetchone()
        if session is None:
            raise ValueError("Browser session is unavailable.")
        valid = connection.execute(
            "SELECT conversation_id FROM conversations WHERE conversation_id = ? AND user_id IS NULL AND channel = 'local_web'",
            (conversation_id,),
        ).fetchone()
        if valid is None:
            raise ValueError("Conversation is not a local guest conversation.")
        connection.execute(
            "INSERT OR IGNORE INTO local_web_session_conversations (token_hash, conversation_id, created_at) VALUES (?, ?, ?)",
            (token_hash, conversation_id, _now()),
        )
        if make_active:
            connection.execute(
                "UPDATE local_web_sessions SET active_conversation_id = ?, updated_at = ? WHERE token_hash = ?",
                (conversation_id, _now(), token_hash),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def set_active_conversation(token_hash: str, conversation_id: str) -> dict | None:
    connection = get_db_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT c.conversation_id, c.status
               FROM local_web_session_conversations sc
               JOIN local_web_sessions s ON s.token_hash = sc.token_hash
               JOIN conversations c ON c.conversation_id = sc.conversation_id
               WHERE sc.token_hash = ? AND sc.conversation_id = ?
                 AND s.expires_at > ? AND c.user_id IS NULL AND c.channel = 'local_web'""",
            (token_hash, conversation_id, _now()),
        ).fetchone()
        if row is None:
            connection.rollback()
            return None
        connection.execute(
            "UPDATE local_web_sessions SET active_conversation_id = ?, updated_at = ? WHERE token_hash = ?",
            (conversation_id, _now(), token_hash),
        )
        connection.commit()
        return {"conversation_id": row[0], "status": row[1]}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_conversations(token_hash: str) -> list[dict]:
    connection = get_db_connection()
    try:
        cursor = connection.execute(
            """SELECT c.conversation_id, c.status, c.created_at, c.updated_at, c.closed_at,
                      (SELECT substr(m.content, 1, 100) FROM conversation_messages m
                       WHERE m.conversation_id = c.conversation_id AND m.role = 'user'
                       ORDER BY m.sequence_number ASC LIMIT 1) AS first_message
               FROM local_web_session_conversations sc
               JOIN local_web_sessions s ON s.token_hash = sc.token_hash
               JOIN conversations c ON c.conversation_id = sc.conversation_id
               WHERE sc.token_hash = ? AND s.expires_at > ?
                 AND c.user_id IS NULL AND c.channel = 'local_web'
               ORDER BY c.updated_at DESC, sc.created_at DESC""",
            (token_hash, _now()),
        )
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        connection.close()


def is_owned_conversation(token_hash: str, conversation_id: str) -> bool:
    connection = get_db_connection()
    try:
        return connection.execute(
            """SELECT 1 FROM local_web_session_conversations sc
               JOIN local_web_sessions s ON s.token_hash = sc.token_hash
               JOIN conversations c ON c.conversation_id = sc.conversation_id
               WHERE sc.token_hash = ? AND sc.conversation_id = ? AND s.expires_at > ?
                 AND c.user_id IS NULL AND c.channel = 'local_web'""",
            (token_hash, conversation_id, _now()),
        ).fetchone() is not None
    finally:
        connection.close()
