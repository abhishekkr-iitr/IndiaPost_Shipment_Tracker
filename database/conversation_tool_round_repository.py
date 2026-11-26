import json
from datetime import datetime
from uuid import uuid4

from database.db_connection import get_db_connection


VALID_ROUND_STATUSES = {
    "recording",
    "pending",
    "running",
    "completed",
    "failed",
    "interrupted",
}


def _current_timestamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _validate_user_id(user_id):
    if user_id is not None:
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("user_id must be a positive integer or None.")


def _validate_required_text(value, field_name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")


def _validate_positive_integer(value, field_name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


def _row_to_dict(cursor, row):
    if row is None:
        return None
    return dict(zip([column[0] for column in cursor.description], row))


def _get_owned_conversation(cursor, conversation_id, user_id):
    if user_id is None:
        cursor.execute(
            """
            SELECT *
            FROM conversations
            WHERE conversation_id = ?
              AND user_id IS NULL;
            """,
            (conversation_id,),
        )
    else:
        cursor.execute(
            """
            SELECT *
            FROM conversations
            WHERE conversation_id = ?
              AND user_id = ?;
            """,
            (conversation_id, user_id),
        )
    return _row_to_dict(cursor, cursor.fetchone())


def _get_owned_round(cursor, round_id, user_id):
    cursor.execute(
        "SELECT * FROM conversation_tool_rounds WHERE round_id = ?;",
        (round_id,),
    )
    tool_round = _row_to_dict(cursor, cursor.fetchone())
    if tool_round is None:
        return None

    if _get_owned_conversation(
        cursor, tool_round["conversation_id"], user_id
    ) is None:
        return None
    return tool_round


def create_tool_round(
    conversation_id,
    round_number,
    tool_calls,
    request_id=None,
    user_id=None,
):
    """
    Atomically persist one complete assistant tool-call round.

    tool_calls is an ordered list of dictionaries containing:
    tool_call_id, tool_name, arguments, call_content, and optional
    provider_message_id. No tool is executed by this repository.
    """
    _validate_required_text(conversation_id, "conversation_id")
    _validate_positive_integer(round_number, "round_number")
    _validate_user_id(user_id)

    if request_id is not None:
        _validate_required_text(request_id, "request_id")

    if not isinstance(tool_calls, list) or not tool_calls:
        raise ValueError("tool_calls must be a non-empty list.")

    normalized_calls = []
    seen_call_ids = set()

    for index, call in enumerate(tool_calls):
        if not isinstance(call, dict):
            raise ValueError(f"tool_calls[{index}] must be a dictionary.")

        for field in ("tool_call_id", "tool_name", "arguments", "call_content"):
            _validate_required_text(call.get(field), f"tool_calls[{index}].{field}")

        if call["tool_call_id"] in seen_call_ids:
            raise ValueError("tool_call_id values must be unique within a round.")
        seen_call_ids.add(call["tool_call_id"])

        try:
            json.loads(call["arguments"])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"tool_calls[{index}].arguments must contain valid JSON."
            ) from exc

        provider_message_id = call.get("provider_message_id")
        if provider_message_id is not None:
            _validate_required_text(
                provider_message_id,
                f"tool_calls[{index}].provider_message_id",
            )

        normalized_calls.append(
            {
                "tool_call_id": call["tool_call_id"],
                "tool_name": call["tool_name"],
                "arguments": call["arguments"],
                "call_content": call["call_content"],
                "provider_message_id": provider_message_id,
            }
        )

    round_id = str(uuid4())
    current_time = _current_timestamp()
    connection = get_db_connection()

    try:
        cursor = connection.cursor()
        cursor.execute("BEGIN IMMEDIATE;")

        conversation = _get_owned_conversation(
            cursor, conversation_id, user_id
        )
        if conversation is None:
            raise ValueError("Conversation not found or access not permitted.")
        if conversation["status"] != "active":
            raise ValueError("Cannot create a tool round for a closed conversation.")

        if request_id is not None:
            cursor.execute(
                """
                SELECT request_id
                FROM conversation_requests
                WHERE request_id = ?
                  AND conversation_id = ?;
                """,
                (request_id, conversation_id),
            )
            if cursor.fetchone() is None:
                raise ValueError("Request not found in the specified conversation.")

        cursor.execute(
            """
            INSERT INTO conversation_tool_rounds (
                round_id, conversation_id, request_id, round_number,
                status, expected_call_count, created_at, updated_at,
                completed_at
            )
            VALUES (?, ?, ?, ?, 'recording', ?, ?, ?, NULL);
            """,
            (
                round_id,
                conversation_id,
                request_id,
                round_number,
                len(normalized_calls),
                current_time,
                current_time,
            ),
        )

        executions = []
        for call in normalized_calls:
            cursor.execute(
                """
                SELECT COALESCE(MAX(sequence_number), 0) + 1
                FROM conversation_messages
                WHERE conversation_id = ?;
                """,
                (conversation_id,),
            )
            sequence_number = cursor.fetchone()[0]

            cursor.execute(
                """
                INSERT INTO conversation_messages (
                    conversation_id, sequence_number, role, content,
                    tool_name, tool_call_id, provider_message_id, created_at
                )
                VALUES (?, ?, 'assistant', ?, ?, ?, ?, ?);
                """,
                (
                    conversation_id,
                    sequence_number,
                    call["call_content"],
                    call["tool_name"],
                    call["tool_call_id"],
                    call["provider_message_id"],
                    current_time,
                ),
            )
            call_message_id = cursor.lastrowid
            tool_execution_id = str(uuid4())

            cursor.execute(
                """
                INSERT INTO conversation_tool_calls (
                    tool_execution_id, conversation_id, request_id, round_id,
                    round_number, tool_call_id, tool_name, arguments,
                    call_message_id, output_message_id, status, error_code,
                    created_at, started_at, updated_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'pending',
                        NULL, ?, NULL, ?, NULL);
                """,
                (
                    tool_execution_id,
                    conversation_id,
                    request_id,
                    round_id,
                    round_number,
                    call["tool_call_id"],
                    call["tool_name"],
                    call["arguments"],
                    call_message_id,
                    current_time,
                    current_time,
                ),
            )

            executions.append(
                {
                    "tool_execution_id": tool_execution_id,
                    "tool_call_id": call["tool_call_id"],
                    "tool_name": call["tool_name"],
                    "call_message_id": call_message_id,
                    "sequence_number": sequence_number,
                    "status": "pending",
                }
            )

        cursor.execute(
            """
            UPDATE conversation_tool_rounds
            SET status = 'pending', updated_at = ?
            WHERE round_id = ? AND status = 'recording';
            """,
            (current_time, round_id),
        )

        cursor.execute(
            """
            UPDATE conversations
            SET updated_at = ?
            WHERE conversation_id = ?;
            """,
            (current_time, conversation_id),
        )

        connection.commit()
        return {
            "round_id": round_id,
            "conversation_id": conversation_id,
            "request_id": request_id,
            "round_number": round_number,
            "status": "pending",
            "expected_call_count": len(normalized_calls),
            "executions": executions,
        }

    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_tool_round(round_id, user_id=None):
    _validate_required_text(round_id, "round_id")
    _validate_user_id(user_id)

    connection = get_db_connection()
    try:
        cursor = connection.cursor()
        tool_round = _get_owned_round(cursor, round_id, user_id)
        if tool_round is None:
            return None

        cursor.execute(
            """
            SELECT *
            FROM conversation_tool_calls
            WHERE round_id = ?
            ORDER BY created_at, tool_execution_id;
            """,
            (round_id,),
        )
        columns = [column[0] for column in cursor.description]
        tool_round["executions"] = [
            dict(zip(columns, row)) for row in cursor.fetchall()
        ]
        return tool_round
    finally:
        connection.close()


def list_tool_rounds(
    conversation_id,
    user_id=None,
    request_id=None,
    limit=100,
):
    _validate_required_text(conversation_id, "conversation_id")
    _validate_user_id(user_id)
    if request_id is not None:
        _validate_required_text(request_id, "request_id")
    _validate_positive_integer(limit, "limit")

    connection = get_db_connection()
    try:
        cursor = connection.cursor()
        if _get_owned_conversation(cursor, conversation_id, user_id) is None:
            return []

        query = """
            SELECT *
            FROM conversation_tool_rounds
            WHERE conversation_id = ?
        """
        parameters = [conversation_id]
        if request_id is not None:
            query += " AND request_id = ?"
            parameters.append(request_id)
        query += " ORDER BY round_number, created_at LIMIT ?;"
        parameters.append(limit)

        cursor.execute(query, parameters)
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        connection.close()


def start_tool_round(round_id, user_id=None):
    """Move a fully recorded pending round to running."""
    _validate_required_text(round_id, "round_id")
    _validate_user_id(user_id)

    connection = get_db_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("BEGIN IMMEDIATE;")
        tool_round = _get_owned_round(cursor, round_id, user_id)
        if tool_round is None:
            raise ValueError("Tool round not found or access not permitted.")
        if tool_round["status"] != "pending":
            raise ValueError("Only pending tool rounds can be started.")

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM conversation_tool_calls
            WHERE round_id = ?;
            """,
            (round_id,),
        )
        if cursor.fetchone()[0] != tool_round["expected_call_count"]:
            raise ValueError("Tool round is incomplete and cannot be started.")

        current_time = _current_timestamp()
        cursor.execute(
            """
            UPDATE conversation_tool_rounds
            SET status = 'running', updated_at = ?
            WHERE round_id = ? AND status = 'pending';
            """,
            (current_time, round_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Tool round status could not be updated.")
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def complete_tool_round(round_id, user_id=None):
    """Complete a running round only when every expected tool call completed."""
    _validate_required_text(round_id, "round_id")
    _validate_user_id(user_id)

    connection = get_db_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("BEGIN IMMEDIATE;")
        tool_round = _get_owned_round(cursor, round_id, user_id)
        if tool_round is None:
            raise ValueError("Tool round not found or access not permitted.")
        if tool_round["status"] == "completed":
            connection.commit()
            return True
        if tool_round["status"] != "running":
            raise ValueError("Only running tool rounds can be completed.")

        cursor.execute(
            """
            SELECT
                COUNT(*) AS total_count,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END)
                    AS completed_count
            FROM conversation_tool_calls
            WHERE round_id = ?;
            """,
            (round_id,),
        )
        row = cursor.fetchone()
        if row[0] != tool_round["expected_call_count"] or row[1] != row[0]:
            raise ValueError("Not all expected tool executions are completed.")

        current_time = _current_timestamp()
        cursor.execute(
            """
            UPDATE conversation_tool_rounds
            SET status = 'completed', updated_at = ?, completed_at = ?
            WHERE round_id = ? AND status = 'running';
            """,
            (current_time, current_time, round_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Tool round status could not be updated.")
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def set_tool_round_status(
    round_id,
    status,
    user_id=None,
    error_code=None,
):
    """Explicitly mark a round failed or interrupted; never replays tools."""
    _validate_required_text(round_id, "round_id")
    _validate_user_id(user_id)
    if status not in {"failed", "interrupted"}:
        raise ValueError("status must be 'failed' or 'interrupted'.")
    if error_code is not None:
        _validate_required_text(error_code, "error_code")

    connection = get_db_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("BEGIN IMMEDIATE;")
        tool_round = _get_owned_round(cursor, round_id, user_id)
        if tool_round is None:
            raise ValueError("Tool round not found or access not permitted.")

        if tool_round["status"] == status:
            connection.commit()
            return True
        if tool_round["status"] not in {"pending", "running"}:
            raise ValueError("Tool round cannot make the requested transition.")

        current_time = _current_timestamp()
        cursor.execute(
            """
            UPDATE conversation_tool_rounds
            SET status = ?, updated_at = ?, completed_at = ?
            WHERE round_id = ?;
            """,
            (status, current_time, current_time, round_id),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_request_recovery_status(request_id, user_id=None):
    """
    Return a read-only recovery snapshot for one request.

    This function never changes request, round, or tool-execution state.
    """
    _validate_required_text(request_id, "request_id")
    _validate_user_id(user_id)

    connection = get_db_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT *
            FROM conversation_requests
            WHERE request_id = ?;
            """,
            (request_id,),
        )
        request = _row_to_dict(cursor, cursor.fetchone())
        if request is None:
            return None

        if _get_owned_conversation(
            cursor, request["conversation_id"], user_id
        ) is None:
            return None

        cursor.execute(
            """
            SELECT *
            FROM conversation_tool_rounds
            WHERE conversation_id = ? AND request_id = ?
            ORDER BY round_number;
            """,
            (request["conversation_id"], request_id),
        )
        round_columns = [column[0] for column in cursor.description]
        rounds = [dict(zip(round_columns, row)) for row in cursor.fetchall()]

        cursor.execute(
            """
            SELECT status, COUNT(*)
            FROM conversation_tool_calls
            WHERE conversation_id = ? AND request_id = ?
            GROUP BY status;
            """,
            (request["conversation_id"], request_id),
        )
        execution_counts = {
            status: count for status, count in cursor.fetchall()
        }

        if request["status"] == "completed":
            recommendation = "request_completed"
        elif any(r["status"] == "interrupted" for r in rounds) or execution_counts.get("interrupted", 0):
            recommendation = "manual_review_uncertain_outcome"
        elif any(r["status"] == "running" for r in rounds) or execution_counts.get("running", 0):
            recommendation = "inspect_running_executions"
        elif any(r["status"] == "failed" for r in rounds) or execution_counts.get("failed", 0):
            recommendation = "manual_review_failed_execution"
        elif request["status"] == "failed":
            recommendation = "assess_request_failure_before_retry"
        elif request["status"] == "interrupted":
            recommendation = "manual_recovery_assessment"
        elif rounds and all(r["status"] == "completed" for r in rounds):
            recommendation = "all_tools_completed_check_for_final_response"
        elif any(r["status"] == "pending" for r in rounds):
            recommendation = "pending_round_requires_controlled_execution"
        elif any(r["status"] == "recording" for r in rounds):
            recommendation = "inspect_incomplete_round_recording"
        elif request["status"] == "processing" and not rounds:
            recommendation = "inspect_request_without_tool_rounds"
        else:
            recommendation = "manual_recovery_assessment"

        return {
            "request": request,
            "rounds": rounds,
            "execution_counts": execution_counts,
            "recommendation": recommendation,
        }
    finally:
        connection.close()


def fail_pending_tool_round(
    round_id,
    user_id=None,
    error_code="manual_recovery_failed",
):
    """
    Explicitly abandon a pending round that has not started.

    All executions must still be pending. No tool is invoked or replayed.
    The round, its executions, and its associated processing request are
    changed in one SQLite transaction.
    """
    _validate_required_text(round_id, "round_id")
    _validate_user_id(user_id)
    _validate_required_text(error_code, "error_code")

    connection = get_db_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("BEGIN IMMEDIATE;")

        tool_round = _get_owned_round(cursor, round_id, user_id)
        if tool_round is None:
            raise ValueError("Tool round not found or access not permitted.")

        if tool_round["status"] == "failed":
            cursor.execute(
                """
                SELECT COUNT(*), SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)
                FROM conversation_tool_calls
                WHERE round_id = ?;
                """,
                (round_id,),
            )
            total, failed = cursor.fetchone()
            if (
                total == tool_round["expected_call_count"]
                and failed == total
            ):
                connection.commit()
                return True
            raise ValueError("Failed round has inconsistent execution states.")

        if tool_round["status"] != "pending":
            raise ValueError("Only a pending tool round can be failed safely.")

        cursor.execute(
            """
            SELECT COUNT(*), SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END)
            FROM conversation_tool_calls
            WHERE round_id = ?;
            """,
            (round_id,),
        )
        total, pending = cursor.fetchone()
        if (
            total != tool_round["expected_call_count"]
            or pending != total
        ):
            raise ValueError(
                "Pending round contains executions that may already have started."
            )

        current_time = _current_timestamp()
        cursor.execute(
            """
            UPDATE conversation_tool_calls
            SET status = 'failed', error_code = ?,
                updated_at = ?, completed_at = ?
            WHERE round_id = ? AND status = 'pending';
            """,
            (error_code, current_time, current_time, round_id),
        )
        cursor.execute(
            """
            UPDATE conversation_tool_rounds
            SET status = 'failed', updated_at = ?, completed_at = ?
            WHERE round_id = ? AND status = 'pending';
            """,
            (current_time, current_time, round_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Tool round status could not be updated.")

        if tool_round["request_id"] is not None:
            cursor.execute(
                """
                UPDATE conversation_requests
                SET status = 'failed', error_code = ?, updated_at = ?,
                    completed_at = ?
                WHERE request_id = ? AND status = 'processing';
                """,
                (
                    error_code,
                    current_time,
                    current_time,
                    tool_round["request_id"],
                ),
            )
            if cursor.rowcount != 1:
                cursor.execute(
                    """
                    SELECT status, error_code
                    FROM conversation_requests
                    WHERE request_id = ?;
                    """,
                    (tool_round["request_id"],),
                )
                request_state = cursor.fetchone()
                if request_state != ("failed", error_code):
                    raise ValueError(
                        "Associated request is not in a recoverable processing state."
                    )

        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def interrupt_running_tool_round(
    round_id,
    user_id=None,
    error_code="manual_recovery_interrupted",
):
    """
    Explicitly stop recovery of a running round without replaying tools.

    Running executions become interrupted (outcome uncertain); executions
    still pending become failed (known not to have started). Completed
    executions are preserved. The associated processing request is marked
    interrupted in the same transaction.
    """
    _validate_required_text(round_id, "round_id")
    _validate_user_id(user_id)
    _validate_required_text(error_code, "error_code")

    connection = get_db_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("BEGIN IMMEDIATE;")

        tool_round = _get_owned_round(cursor, round_id, user_id)
        if tool_round is None:
            raise ValueError("Tool round not found or access not permitted.")

        if tool_round["status"] == "interrupted":
            connection.commit()
            return True

        if tool_round["status"] != "running":
            raise ValueError("Only a running tool round can be interrupted.")

        current_time = _current_timestamp()
        cursor.execute(
            """
            UPDATE conversation_tool_calls
            SET status = 'interrupted', error_code = ?,
                updated_at = ?, completed_at = ?
            WHERE round_id = ? AND status = 'running';
            """,
            (error_code, current_time, current_time, round_id),
        )
        cursor.execute(
            """
            UPDATE conversation_tool_calls
            SET status = 'failed', error_code = 'not_started_before_interruption',
                updated_at = ?, completed_at = ?
            WHERE round_id = ? AND status = 'pending';
            """,
            (current_time, current_time, round_id),
        )
        cursor.execute(
            """
            UPDATE conversation_tool_rounds
            SET status = 'interrupted', updated_at = ?, completed_at = ?
            WHERE round_id = ? AND status = 'running';
            """,
            (current_time, current_time, round_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Tool round status could not be updated.")

        if tool_round["request_id"] is not None:
            cursor.execute(
                """
                UPDATE conversation_requests
                SET status = 'interrupted', error_code = ?, updated_at = ?,
                    completed_at = ?
                WHERE request_id = ? AND status = 'processing';
                """,
                (
                    error_code,
                    current_time,
                    current_time,
                    tool_round["request_id"],
                ),
            )
            if cursor.rowcount != 1:
                cursor.execute(
                    """
                    SELECT status, error_code
                    FROM conversation_requests
                    WHERE request_id = ?;
                    """,
                    (tool_round["request_id"],),
                )
                request_state = cursor.fetchone()
                if request_state != ("interrupted", error_code):
                    raise ValueError(
                        "Associated request is not in a recoverable processing state."
                    )

        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
