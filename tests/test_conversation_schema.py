
import os
import sqlite3
import tempfile
from pathlib import Path


def run_test():

    previous_db_path = os.environ.get("INDIAPOST_DB_PATH")

    try:
        with tempfile.TemporaryDirectory(
            prefix="IndiaPost_ConversationSchema_Test_"
        ) as temp_dir:

            test_db_path = Path(temp_dir) / "test_conversations.db"

            os.environ["INDIAPOST_DB_PATH"] = str(test_db_path)

            # Import after setting the temporary database path.
            from database.schema import (
                create_users_table,
                create_conversations_table,
                create_conversation_messages_table,
            )

            from database.db_connection import get_db_connection

            # Create tables in dependency order.
            create_users_table()
            create_conversations_table()
            create_conversation_messages_table()

            # Verify that creation is safe to repeat.
            create_conversations_table()
            create_conversation_messages_table()

            connection = get_db_connection()

            try:
                cursor = connection.cursor()

                # TEST 1: Verify all three required tables exist.
                cursor.execute("""
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table';
                """)

                existing_tables = {
                    row[0] for row in cursor.fetchall()
                }

                expected_tables = {
                    "users",
                    "conversations",
                    "conversation_messages",
                }

                assert expected_tables.issubset(existing_tables)

                print("TEST 1 PASSED: Required tables exist.")

                # TEST 2: Verify foreign-key enforcement.
                cursor.execute("PRAGMA foreign_keys;")

                assert cursor.fetchone()[0] == 1

                print("TEST 2 PASSED: Foreign keys are enabled.")

                # TEST 3: Verify conversation columns.
                cursor.execute(
                    "PRAGMA table_info(conversations);"
                )

                conversation_columns = {
                    row[1] for row in cursor.fetchall()
                }

                expected_conversation_columns = {
                    "conversation_id",
                    "user_id",
                    "channel",
                    "channel_conversation_id",
                    "status",
                    "provider",
                    "provider_conversation_id",
                    "created_at",
                    "updated_at",
                    "closed_at",
                }

                assert expected_conversation_columns.issubset(
                    conversation_columns
                )

                print("TEST 3 PASSED: Conversation columns verified.")

                # TEST 4: Verify message columns.
                cursor.execute(
                    "PRAGMA table_info(conversation_messages);"
                )

                message_columns = {
                    row[1] for row in cursor.fetchall()
                }

                expected_message_columns = {
                    "message_id",
                    "conversation_id",
                    "sequence_number",
                    "role",
                    "content",
                    "tool_name",
                    "tool_call_id",
                    "provider_message_id",
                    "created_at",
                }

                assert expected_message_columns.issubset(
                    message_columns
                )

                print("TEST 4 PASSED: Message columns verified.")

                # TEST 5: Verify foreign-key relationships.
                cursor.execute(
                    "PRAGMA foreign_key_list(conversations);"
                )

                conversation_foreign_keys = cursor.fetchall()

                assert any(
                    row[2] == "users"
                    and row[3] == "user_id"
                    and row[4] == "user_id"
                    and row[6].upper() == "SET NULL"
                    for row in conversation_foreign_keys
                )

                cursor.execute(
                    "PRAGMA foreign_key_list(conversation_messages);"
                )

                message_foreign_keys = cursor.fetchall()

                assert any(
                    row[2] == "conversations"
                    and row[3] == "conversation_id"
                    and row[4] == "conversation_id"
                    and row[6].upper() == "CASCADE"
                    for row in message_foreign_keys
                )

                print("TEST 5 PASSED: Foreign-key relationships verified.")

                # TEST 6: Insert a registered user and conversation.
                cursor.execute("""
                    INSERT INTO users (
                        external_user_id,
                        created_at
                    )
                    VALUES (?, ?);
                """, ("TEST_USER_001", "2026-11-13T12:00:00+05:30"))

                user_id = cursor.lastrowid

                cursor.execute("""
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
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    "test-conversation-001",
                    user_id,
                    "telegram",
                    "test-chat-001",
                    "active",
                    "openai",
                    "2026-11-13T12:00:00+05:30",
                    "2026-11-13T12:00:00+05:30",
                ))

                connection.commit()

                print("TEST 6 PASSED: Registered conversation inserted.")

                # TEST 7: Insert messages and verify ordering.
                cursor.execute("""
                    INSERT INTO conversation_messages (
                        conversation_id,
                        sequence_number,
                        role,
                        content,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?);
                """, (
                    "test-conversation-001",
                    2,
                    "assistant",
                    "Your shipment is in transit.",
                    "2026-11-13T12:00:02+05:30",
                ))

                cursor.execute("""
                    INSERT INTO conversation_messages (
                        conversation_id,
                        sequence_number,
                        role,
                        content,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?);
                """, (
                    "test-conversation-001",
                    1,
                    "user",
                    "Track my shipment.",
                    "2026-11-13T12:00:01+05:30",
                ))

                connection.commit()

                cursor.execute("""
                    SELECT sequence_number, role
                    FROM conversation_messages
                    WHERE conversation_id = ?
                    ORDER BY sequence_number;
                """, ("test-conversation-001",))

                ordered_messages = cursor.fetchall()

                assert ordered_messages == [
                    (1, "user"),
                    (2, "assistant"),
                ]

                print("TEST 7 PASSED: Messages are retrieved in order.")

                # TEST 8: Verify duplicate sequence numbers are rejected.
                try:
                    cursor.execute("""
                        INSERT INTO conversation_messages (
                            conversation_id,
                            sequence_number,
                            role,
                            content,
                            created_at
                        )
                        VALUES (?, ?, ?, ?, ?);
                    """, (
                        "test-conversation-001",
                        1,
                        "user",
                        "Duplicate sequence test.",
                        "2026-11-13T12:00:03+05:30",
                    ))

                    connection.commit()

                    raise AssertionError(
                        "Duplicate sequence number was accepted."
                    )

                except sqlite3.IntegrityError:
                    connection.rollback()

                print("TEST 8 PASSED: Duplicate sequence numbers rejected.")

                # TEST 9: Verify invalid message roles are rejected.
                try:
                    cursor.execute("""
                        INSERT INTO conversation_messages (
                            conversation_id,
                            sequence_number,
                            role,
                            content,
                            created_at
                        )
                        VALUES (?, ?, ?, ?, ?);
                    """, (
                        "test-conversation-001",
                        3,
                        "invalid_role",
                        "Invalid role test.",
                        "2026-11-13T12:00:03+05:30",
                    ))

                    connection.commit()

                    raise AssertionError(
                        "Invalid message role was accepted."
                    )

                except sqlite3.IntegrityError:
                    connection.rollback()

                print("TEST 9 PASSED: Invalid message roles rejected.")

                # TEST 10: Verify deleting a user preserves conversations.
                cursor.execute(
                    "DELETE FROM users WHERE user_id = ?;",
                    (user_id,)
                )

                connection.commit()

                cursor.execute("""
                    SELECT user_id
                    FROM conversations
                    WHERE conversation_id = ?;
                """, ("test-conversation-001",))

                conversation_owner = cursor.fetchone()

                assert conversation_owner == (None,)

                print("TEST 10 PASSED: Conversation retained after user deletion.")

                # TEST 11: Verify deleting a conversation deletes its messages.
                cursor.execute("""
                    DELETE FROM conversations
                    WHERE conversation_id = ?;
                """, ("test-conversation-001",))

                connection.commit()

                cursor.execute("""
                    SELECT COUNT(*)
                    FROM conversation_messages
                    WHERE conversation_id = ?;
                """, ("test-conversation-001",))

                remaining_messages = cursor.fetchone()[0]

                assert remaining_messages == 0

                print("TEST 11 PASSED: Conversation deletion cascades to messages.")

                # TEST 12: Verify database integrity.
                cursor.execute("PRAGMA integrity_check;")

                integrity_result = cursor.fetchone()[0]

                assert integrity_result == "ok"

                cursor.execute("PRAGMA foreign_key_check;")

                foreign_key_errors = cursor.fetchall()

                assert not foreign_key_errors

                print("TEST 12 PASSED: Database integrity verified.")

            finally:
                connection.close()

            print("\nCONVERSATION SCHEMA TEST: PASSED")

    finally:
        if previous_db_path is None:
            os.environ.pop("INDIAPOST_DB_PATH", None)
        else:
            os.environ["INDIAPOST_DB_PATH"] = previous_db_path


if __name__ == "__main__":
    run_test()
