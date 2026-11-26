
import json
from datetime import datetime
from uuid import uuid4

from database.db_connection import get_db_connection


VALID_TOOL_STATUSES = {
    "pending",
    "running",
    "completed",
    "failed",
    "interrupted",
}


def _current_timestamp():
    """Return a timezone-aware ISO 8601 timestamp."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _validate_user_id(user_id):
    """Validate a registered user's internal database ID."""
    if user_id is not None:
        if isinstance(user_id, bool) or not isinstance(user_id, int):
            raise ValueError("user_id must be a positive integer or None.")

        if user_id <= 0:
            raise ValueError("user_id must be a positive integer or None.")


def _validate_required_text(value, field_name):
    """Validate a required non-empty string."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")


def _row_to_dict(cursor, row):
    """Convert a SQLite row into a dictionary."""
    if row is None:
        return None

    columns = [column[0] for column in cursor.description]
    return dict(zip(columns, row))


def _get_owned_conversation(cursor, conversation_id, user_id):
    """Retrieve a conversation belonging to the supplied user or guest."""

    if user_id is None:
        cursor.execute(
            """
            SELECT *
            FROM conversations
            WHERE conversation_id = ?
              AND user_id IS NULL;
            """,
            (conversation_id,)
        )
    else:
        cursor.execute(
            """
            SELECT *
            FROM conversations
            WHERE conversation_id = ?
              AND user_id = ?;
            """,
            (conversation_id, user_id)
        )

    return _row_to_dict(cursor, cursor.fetchone())


def _get_owned_execution(cursor, tool_execution_id, user_id):
    """Retrieve an execution only through its owned conversation."""

    cursor.execute(
        """
        SELECT *
        FROM conversation_tool_calls
        WHERE tool_execution_id = ?;
        """,
        (tool_execution_id,)
    )

    execution = _row_to_dict(cursor, cursor.fetchone())

    if execution is None:
        return None

    conversation = _get_owned_conversation(
        cursor,
        execution["conversation_id"],
        user_id
    )

    if conversation is None:
        return None

    return execution


def _validate_round_number(round_number):
    if (
        isinstance(round_number, bool)
        or not isinstance(round_number, int)
        or round_number <= 0
    ):
        raise ValueError("round_number must be a positive integer.")


def create_tool_execution(
    conversation_id,
    tool_call_id,
    tool_name,
    arguments,
    call_content,
    round_number,
    request_id=None,
    user_id=None,
    provider_message_id=None
):
    """
    Atomically create an assistant tool-call message and pending execution.

    The assistant message and execution record are stored in one
    transaction so they cannot be separated by a partial database write.
    """

    _validate_required_text(conversation_id, "conversation_id")
    _validate_required_text(tool_call_id, "tool_call_id")
    _validate_required_text(tool_name, "tool_name")
    _validate_required_text(arguments, "arguments")
    _validate_required_text(call_content, "call_content")
    _validate_round_number(round_number)
    _validate_user_id(user_id)

    if request_id is not None:
        _validate_required_text(request_id, "request_id")

    if provider_message_id is not None:
        _validate_required_text(provider_message_id, "provider_message_id")

    # Ensure arguments contain valid JSON.
    try:
        json.loads(arguments)
    except (TypeError, ValueError) as exc:
        raise ValueError("arguments must contain valid JSON.") from exc

    tool_execution_id = str(uuid4())
    current_time = _current_timestamp()

    connection = get_db_connection()

    try:
        cursor = connection.cursor()
        cursor.execute("BEGIN IMMEDIATE;")

        conversation = _get_owned_conversation(
            cursor,
            conversation_id,
            user_id
        )

        if conversation is None:
            raise ValueError(
                "Conversation not found or access not permitted."
            )

        if conversation["status"] != "active":
            raise ValueError(
                "Cannot create a tool execution for a closed conversation."
            )

        if request_id is not None:
            cursor.execute(
                """
                SELECT request_id
                FROM conversation_requests
                WHERE request_id = ?
                  AND conversation_id = ?;
                """,
                (request_id, conversation_id)
            )

            if cursor.fetchone() is None:
                raise ValueError(
                    "Request not found in the specified conversation."
                )

        # Allocate the next message sequence number.
        cursor.execute(
            """
            SELECT COALESCE(MAX(sequence_number), 0) + 1
            FROM conversation_messages
            WHERE conversation_id = ?;
            """,
            (conversation_id,)
        )

        sequence_number = cursor.fetchone()[0]

        # Save the assistant's tool-call message.
        cursor.execute(
            """
            INSERT INTO conversation_messages (
                conversation_id,
                sequence_number,
                role,
                content,
                tool_name,
                tool_call_id,
                provider_message_id,
                created_at
            )
            VALUES (?, ?, 'assistant', ?, ?, ?, ?, ?);
            """,
            (
                conversation_id,
                sequence_number,
                call_content,
                tool_name,
                tool_call_id,
                provider_message_id,
                current_time
            )
        )

        call_message_id = cursor.lastrowid

        # Save the pending execution record.
        cursor.execute(
            """
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
                error_code,
                created_at,
                started_at,
                updated_at,
                completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'pending',
                    NULL, ?, NULL, ?, NULL);
            """,
            (
                tool_execution_id,
                conversation_id,
                request_id,
                round_number,
                tool_call_id,
                tool_name,
                arguments,
                call_message_id,
                current_time,
                current_time
            )
        )

        cursor.execute(
            """
            UPDATE conversations
            SET updated_at = ?
            WHERE conversation_id = ?;
            """,
            (current_time, conversation_id)
        )

        cursor.execute(
            """
            SELECT *
            FROM conversation_tool_calls
            WHERE tool_execution_id = ?;
            """,
            (tool_execution_id,)
        )

        execution = _row_to_dict(cursor, cursor.fetchone())

        connection.commit()

        execution["call_message_id"] = call_message_id
        execution["sequence_number"] = sequence_number

        return execution

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


def get_tool_execution(tool_execution_id, user_id=None):
    """Retrieve one execution after validating conversation ownership."""

    _validate_required_text(tool_execution_id, "tool_execution_id")
    _validate_user_id(user_id)

    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        return _get_owned_execution(
            cursor,
            tool_execution_id,
            user_id
        )

    finally:
        connection.close()


def list_tool_executions(
    conversation_id,
    user_id=None,
    request_id=None,
    limit=100
):
    """List tool executions for an owned conversation."""

    _validate_required_text(conversation_id, "conversation_id")
    _validate_user_id(user_id)

    if request_id is not None:
        _validate_required_text(request_id, "request_id")

    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer.")

    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        conversation = _get_owned_conversation(
            cursor,
            conversation_id,
            user_id
        )

        if conversation is None:
            return []

        query = """
            SELECT *
            FROM conversation_tool_calls
            WHERE conversation_id = ?
        """

        parameters = [conversation_id]

        if request_id is not None:
            query += " AND request_id = ?"
            parameters.append(request_id)

        query += """
            ORDER BY round_number ASC, created_at ASC,
                     tool_execution_id ASC
            LIMIT ?;
        """

        parameters.append(limit)

        cursor.execute(query, parameters)

        columns = [column[0] for column in cursor.description]

        return [
            dict(zip(columns, row))
            for row in cursor.fetchall()
        ]

    finally:
        connection.close()


def start_tool_execution(tool_execution_id, user_id=None):
    """
    Change a pending execution to running.

    This must be committed before the actual tool is invoked.
    Running and interrupted executions are never automatically restarted.
    """

    _validate_required_text(tool_execution_id, "tool_execution_id")
    _validate_user_id(user_id)

    connection = get_db_connection()

    try:
        cursor = connection.cursor()
        cursor.execute("BEGIN IMMEDIATE;")

        execution = _get_owned_execution(
            cursor,
            tool_execution_id,
            user_id
        )

        if execution is None:
            raise ValueError(
                "Tool execution not found or access not permitted."
            )

        if execution["status"] != "pending":
            raise ValueError(
                "Only pending tool executions can be started."
            )

        current_time = _current_timestamp()

        cursor.execute(
            """
            UPDATE conversation_tool_calls
            SET
                status = 'running',
                started_at = ?,
                updated_at = ?
            WHERE tool_execution_id = ?
              AND status = 'pending';
            """,
            (
                current_time,
                current_time,
                tool_execution_id
            )
        )

        if cursor.rowcount != 1:
            raise ValueError("Tool execution status could not be updated.")

        connection.commit()
        return True

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


def complete_tool_execution(
    tool_execution_id,
    output_content,
    user_id=None,
    provider_message_id=None
):
    """
    Atomically store a tool output and mark its execution completed.

    The output message and completed status are committed together.
    """

    _validate_required_text(tool_execution_id, "tool_execution_id")
    _validate_user_id(user_id)

    if not isinstance(output_content, str):
        raise ValueError("output_content must be a string.")

    if provider_message_id is not None:
        _validate_required_text(provider_message_id, "provider_message_id")

    connection = get_db_connection()

    try:
        cursor = connection.cursor()
        cursor.execute("BEGIN IMMEDIATE;")

        execution = _get_owned_execution(
            cursor,
            tool_execution_id,
            user_id
        )

        if execution is None:
            raise ValueError(
                "Tool execution not found or access not permitted."
            )

        # Permit an identical repeat of an already completed operation.
        if execution["status"] == "completed":
            cursor.execute(
                """
                SELECT content
                FROM conversation_messages
                WHERE conversation_id = ?
                  AND message_id = ?;
                """,
                (
                    execution["conversation_id"],
                    execution["output_message_id"]
                )
            )

            existing_output = cursor.fetchone()

            if existing_output is not None and existing_output[0] == output_content:
                connection.commit()
                return True

            raise ValueError(
                "Tool execution has already completed with another output."
            )

        if execution["status"] != "running":
            raise ValueError(
                "Only running tool executions can be completed."
            )

        conversation_id = execution["conversation_id"]

        cursor.execute(
            """
            SELECT COALESCE(MAX(sequence_number), 0) + 1
            FROM conversation_messages
            WHERE conversation_id = ?;
            """,
            (conversation_id,)
        )

        sequence_number = cursor.fetchone()[0]
        current_time = _current_timestamp()

        # Store the tool output.
        cursor.execute(
            """
            INSERT INTO conversation_messages (
                conversation_id,
                sequence_number,
                role,
                content,
                tool_name,
                tool_call_id,
                provider_message_id,
                created_at
            )
            VALUES (?, ?, 'tool', ?, ?, ?, ?, ?);
            """,
            (
                conversation_id,
                sequence_number,
                output_content,
                execution["tool_name"],
                execution["tool_call_id"],
                provider_message_id,
                current_time
            )
        )

        output_message_id = cursor.lastrowid

        # Mark execution completed in the same transaction.
        cursor.execute(
            """
            UPDATE conversation_tool_calls
            SET
                status = 'completed',
                output_message_id = ?,
                error_code = NULL,
                updated_at = ?,
                completed_at = ?
            WHERE tool_execution_id = ?
              AND status = 'running';
            """,
            (
                output_message_id,
                current_time,
                current_time,
                tool_execution_id
            )
        )

        if cursor.rowcount != 1:
            raise ValueError("Tool execution status could not be updated.")

        cursor.execute(
            """
            UPDATE conversations
            SET updated_at = ?
            WHERE conversation_id = ?;
            """,
            (current_time, conversation_id)
        )

        connection.commit()
        return True

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


def _set_tool_execution_status(
    tool_execution_id,
    new_status,
    allowed_current_statuses,
    user_id=None,
    error_code=None
):
    """Apply a controlled failure or interruption transition."""

    _validate_required_text(tool_execution_id, "tool_execution_id")
    _validate_user_id(user_id)

    if new_status not in {"failed", "interrupted"}:
        raise ValueError("Invalid target status.")

    if error_code is not None:
        _validate_required_text(error_code, "error_code")

    connection = get_db_connection()

    try:
        cursor = connection.cursor()
        cursor.execute("BEGIN IMMEDIATE;")

        execution = _get_owned_execution(
            cursor,
            tool_execution_id,
            user_id
        )

        if execution is None:
            raise ValueError(
                "Tool execution not found or access not permitted."
            )

        if execution["status"] == new_status:
            if execution["error_code"] == error_code:
                connection.commit()
                return True

            raise ValueError(
                "Tool execution already has this status with a different "
                "error code."
            )

        if execution["status"] not in allowed_current_statuses:
            raise ValueError(
                "Tool execution cannot make the requested status transition."
            )

        current_time = _current_timestamp()

        cursor.execute(
            """
            UPDATE conversation_tool_calls
            SET
                status = ?,
                error_code = ?,
                updated_at = ?,
                completed_at = ?
            WHERE tool_execution_id = ?;
            """,
            (
                new_status,
                error_code,
                current_time,
                current_time,
                tool_execution_id
            )
        )

        if cursor.rowcount != 1:
            raise ValueError("Tool execution status could not be updated.")

        connection.commit()
        return True

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


def fail_tool_execution(
    tool_execution_id,
    user_id=None,
    error_code=None
):
    """
    Mark a pending or running execution as failed.

    Use only when the failure is known, rather than when the result
    of an external operation is uncertain.
    """

    return _set_tool_execution_status(
        tool_execution_id,
        "failed",
        {"pending", "running"},
        user_id=user_id,
        error_code=error_code
    )


def interrupt_tool_execution(
    tool_execution_id,
    user_id=None,
    error_code=None
):
    """Mark a running execution as interrupted with an uncertain outcome."""

    return _set_tool_execution_status(
        tool_execution_id,
        "interrupted",
        {"running"},
        user_id=user_id,
        error_code=error_code
    )
