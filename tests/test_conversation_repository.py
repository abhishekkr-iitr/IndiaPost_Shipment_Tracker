
import os
import sqlite3
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor


def run_tests():

    previous_db_path = os.environ.get("INDIAPOST_DB_PATH")

    try:
        with tempfile.TemporaryDirectory(
            prefix="IndiaPost_ConversationRepository_Test_"
        ) as temporary_directory:

            test_db_path = Path(temporary_directory) / "test_conversations.db"

            os.environ["INDIAPOST_DB_PATH"] = str(test_db_path)

            from database.schema import (
                create_users_table,
                create_conversations_table,
                create_conversation_messages_table
            )

            from database.db_connection import get_db_connection

            from database.conversation_repository import (
                create_conversation,
                get_conversation,
                list_user_conversations,
                update_conversation,
                close_conversation,
                add_message,
                get_conversation_messages
            )

            # -------------------------------------------------
            # Test setup
            # -------------------------------------------------

            create_users_table()
            create_conversations_table()
            create_conversation_messages_table()

            connection = get_db_connection()

            cursor = connection.cursor()

            cursor.execute(
                """
                INSERT INTO users (external_user_id, created_at)
                VALUES (?, ?);
                """,
                ("test-user-1", "2026-11-13T10:00:00+05:30")
            )

            user_1_id = cursor.lastrowid

            cursor.execute(
                """
                INSERT INTO users (external_user_id, created_at)
                VALUES (?, ?);
                """,
                ("test-user-2", "2026-11-13T10:00:00+05:30")
            )

            user_2_id = cursor.lastrowid

            connection.commit()
            connection.close()

            print("Test setup completed.")

            # -------------------------------------------------
            # Test 1: Create registered-user conversation
            # -------------------------------------------------

            conversation_id = create_conversation(
                channel="telegram",
                user_id=user_1_id,
                channel_conversation_id="telegram-chat-100",
                provider="openai",
                provider_conversation_id="test-provider-id"
            )

            assert isinstance(conversation_id, str)
            assert len(conversation_id) == 36

            print("TEST 1 PASSED: Registered conversation creation.")

            # -------------------------------------------------
            # Test 2: Retrieve conversation
            # -------------------------------------------------

            conversation = get_conversation(
                conversation_id,
                user_id=user_1_id
            )

            assert conversation is not None
            assert conversation["user_id"] == user_1_id
            assert conversation["channel"] == "telegram"
            assert conversation["status"] == "active"

            print("TEST 2 PASSED: Conversation retrieval.")

            # -------------------------------------------------
            # Test 3: Enforce registered-user ownership
            # -------------------------------------------------

            unauthorized_result = get_conversation(
                conversation_id,
                user_id=user_2_id
            )

            assert unauthorized_result is None

            print("TEST 3 PASSED: Registered-user ownership.")

            # -------------------------------------------------
            # Test 4: Create and retrieve guest conversation
            # -------------------------------------------------

            guest_conversation_id = create_conversation(
                channel="api"
            )

            guest_conversation = get_conversation(
                guest_conversation_id
            )

            assert guest_conversation is not None
            assert guest_conversation["user_id"] is None

            assert get_conversation(
                guest_conversation_id,
                user_id=user_1_id
            ) is None

            print("TEST 4 PASSED: Guest conversation isolation.")

            # -------------------------------------------------
            # Test 5: List registered-user conversations
            # -------------------------------------------------

            conversations = list_user_conversations(user_1_id)

            assert len(conversations) == 1
            assert conversations[0]["conversation_id"] == conversation_id

            print("TEST 5 PASSED: Registered-user conversation listing.")

            # -------------------------------------------------
            # Test 6: Update provider metadata
            # -------------------------------------------------

            update_result = update_conversation(
                conversation_id,
                user_id=user_1_id,
                provider="openai",
                provider_conversation_id="updated-provider-id"
            )

            assert update_result is True

            conversation = get_conversation(
                conversation_id,
                user_id=user_1_id
            )

            assert conversation["provider"] == "openai"
            assert (
                conversation["provider_conversation_id"]
                == "updated-provider-id"
            )

            print("TEST 6 PASSED: Conversation metadata update.")

            # -------------------------------------------------
            # Test 6A: Partial provider metadata updates
            # -------------------------------------------------

            update_result = update_conversation(
                conversation_id,
                user_id=user_1_id,
                provider="updated-provider"
            )

            assert update_result is True

            conversation = get_conversation(
                conversation_id,
                user_id=user_1_id
            )

            assert conversation["provider"] == "updated-provider"
            assert (
                conversation["provider_conversation_id"]
                == "updated-provider-id"
            )

            update_result = update_conversation(
                conversation_id,
                user_id=user_1_id,
                provider_conversation_id="new-provider-id"
            )

            assert update_result is True

            conversation = get_conversation(
                conversation_id,
                user_id=user_1_id
            )

            assert conversation["provider"] == "updated-provider"
            assert (
                conversation["provider_conversation_id"]
                == "new-provider-id"
            )

            # Explicit None must clear a field.
            update_result = update_conversation(
                conversation_id,
                user_id=user_1_id,
                provider_conversation_id=None
            )

            assert update_result is True

            conversation = get_conversation(
                conversation_id,
                user_id=user_1_id
            )

            assert conversation["provider"] == "updated-provider"
            assert conversation["provider_conversation_id"] is None

            print("TEST 6A PASSED: Partial provider metadata updates.")

            # -------------------------------------------------
            # Test 7: Add messages and verify sequence
            # -------------------------------------------------

            first_message = add_message(
                conversation_id,
                role="user",
                content="Where is my parcel?",
                user_id=user_1_id
            )

            second_message = add_message(
                conversation_id,
                role="assistant",
                content="I can help track your parcel.",
                user_id=user_1_id
            )

            third_message = add_message(
                conversation_id,
                role="tool",
                content='{"status": "In Transit"}',
                user_id=user_1_id,
                tool_name="track_india_post",
                tool_call_id="tool-call-001"
            )

            assert first_message["sequence_number"] == 1
            assert second_message["sequence_number"] == 2
            assert third_message["sequence_number"] == 3

            messages = get_conversation_messages(
                conversation_id,
                user_id=user_1_id
            )

            assert len(messages) == 3
            assert [m["sequence_number"] for m in messages] == [1, 2, 3]
            assert messages[0]["role"] == "user"
            assert messages[1]["role"] == "assistant"
            assert messages[2]["role"] == "tool"
            assert messages[2]["tool_name"] == "track_india_post"
            assert messages[2]["tool_call_id"] == "tool-call-001"

            print("TEST 7 PASSED: Message storage and ordering.")

            # -------------------------------------------------
            # Test 7A: Concurrent message insertion
            # -------------------------------------------------

            def insert_concurrent_message(number):
                return add_message(
                    conversation_id,
                    role="user",
                    content=f"Concurrent message {number}",
                    user_id=user_1_id
                )

            concurrent_message_count = 20

            with ThreadPoolExecutor(max_workers=8) as executor:
                concurrent_results = list(
                    executor.map(
                        insert_concurrent_message,
                        range(concurrent_message_count)
                    )
                )

            concurrent_sequences = sorted(
                result["sequence_number"]
                for result in concurrent_results
            )

            expected_sequences = list(
                range(4, 4 + concurrent_message_count)
            )

            assert concurrent_sequences == expected_sequences

            all_messages = get_conversation_messages(
                conversation_id,
                user_id=user_1_id
            )

            assert len(all_messages) == 3 + concurrent_message_count

            actual_sequences = [
                message["sequence_number"]
                for message in all_messages
            ]

            assert actual_sequences == list(
                range(1, 4 + concurrent_message_count)
            )

            print("TEST 7A PASSED: Concurrent message insertion.")

            # -------------------------------------------------
            # Test 8: Enforce message ownership
            # -------------------------------------------------

            try:
                add_message(
                    conversation_id,
                    role="user",
                    content="Unauthorized message",
                    user_id=user_2_id
                )
                raise AssertionError(
                    "Unauthorized message insertion was accepted."
                )

            except ValueError:
                pass

            assert get_conversation_messages(
                conversation_id,
                user_id=user_2_id
            ) == []

            print("TEST 8 PASSED: Message ownership enforcement.")

            # -------------------------------------------------
            # Test 9: Validate message role
            # -------------------------------------------------

            try:
                add_message(
                    conversation_id,
                    role="administrator",
                    content="Invalid role",
                    user_id=user_1_id
                )
                raise AssertionError("Invalid message role was accepted.")

            except ValueError:
                pass

            print("TEST 9 PASSED: Invalid message role rejection.")

            # -------------------------------------------------
            # Test 10: Retrieve limited message history
            # -------------------------------------------------

            limited_messages = get_conversation_messages(
                conversation_id,
                user_id=user_1_id,
                limit=2
            )

            assert len(limited_messages) == 2
            assert limited_messages[0]["sequence_number"] == 1
            assert limited_messages[1]["sequence_number"] == 2

            print("TEST 10 PASSED: Message history limit.")

            # -------------------------------------------------
            # Test 11: Close conversation
            # -------------------------------------------------

            close_result = close_conversation(
                conversation_id,
                user_id=user_1_id
            )

            assert close_result is True

            conversation = get_conversation(
                conversation_id,
                user_id=user_1_id
            )

            assert conversation["status"] == "closed"
            assert conversation["closed_at"] is not None

            print("TEST 11 PASSED: Conversation closing.")

            # -------------------------------------------------
            # Test 12: Prevent messages in closed conversation
            # -------------------------------------------------

            try:
                add_message(
                    conversation_id,
                    role="user",
                    content="This should not be saved.",
                    user_id=user_1_id
                )
                raise AssertionError(
                    "A message was accepted in a closed conversation."
                )

            except ValueError:
                pass

            print("TEST 12 PASSED: Closed conversation protection.")

            # -------------------------------------------------
            # Test 13: Prevent updates to closed conversation
            # -------------------------------------------------

            update_result = update_conversation(
                conversation_id,
                user_id=user_1_id,
                provider="another-provider"
            )

            assert update_result is False

            print("TEST 13 PASSED: Closed conversation update protection.")

            # -------------------------------------------------
            # Test 14: Guest message storage
            # -------------------------------------------------

            guest_message = add_message(
                guest_conversation_id,
                role="user",
                content="Hello",
            )

            assert guest_message["sequence_number"] == 1

            guest_messages = get_conversation_messages(
                guest_conversation_id
            )

            assert len(guest_messages) == 1
            assert guest_messages[0]["content"] == "Hello"

            print("TEST 14 PASSED: Guest message storage.")

            # -------------------------------------------------
            # Test 15: Guest conversation closing
            # -------------------------------------------------

            assert close_conversation(guest_conversation_id) is True

            assert (
                get_conversation(guest_conversation_id)["status"]
                == "closed"
            )

            print("TEST 15 PASSED: Guest conversation closing.")

            # -------------------------------------------------
            # Test 16: Database integrity
            # -------------------------------------------------

            connection = get_db_connection()
            cursor = connection.cursor()

            cursor.execute("PRAGMA integrity_check;")
            integrity_result = cursor.fetchone()[0]

            cursor.execute("PRAGMA foreign_key_check;")
            foreign_key_result = cursor.fetchall()

            connection.close()

            assert integrity_result == "ok"
            assert foreign_key_result == []

            print("TEST 16 PASSED: Database integrity.")

            print()
            print("CONVERSATION REPOSITORY TEST: PASSED")

    finally:
        if previous_db_path is None:
            os.environ.pop("INDIAPOST_DB_PATH", None)
        else:
            os.environ["INDIAPOST_DB_PATH"] = previous_db_path


if __name__ == "__main__":
    run_tests()
