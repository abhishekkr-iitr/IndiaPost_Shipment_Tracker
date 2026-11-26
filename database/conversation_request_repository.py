
import hashlib
from datetime import datetime

from database.db_connection import get_db_connection


VALID_REQUEST_STATUSES = {
    "processing",
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
            raise ValueError(
                "user_id must be a positive integer or None."
            )

        if user_id <= 0:
            raise ValueError(
                "user_id must be a positive integer or None."
            )


def _validate_required_text(value, field_name):
    """Validate a required non-empty string."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{field_name} must be a non-empty string."
        )


def _row_to_dict(cursor, row):
    """Convert a SQLite row into a dictionary."""
    if row is None:
        return None

    columns = [column[0] for column in cursor.description]
    return dict(zip(columns, row))


def _get_owned_conversation(cursor, conversation_id, user_id):
    """
    Retrieve a conversation only when it belongs to the supplied user.

    Passing user_id=None restricts the lookup to guest conversations.
    """
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


def _calculate_request_hash(conversation_id, user_message):
    """
    Create a stable hash for the conversation and exact user message.
    """
    hash_input = (
        conversation_id + "\0" + user_message
    ).encode("utf-8")

    return hashlib.sha256(hash_input).hexdigest()



def create_request(
    request_id,
    conversation_id,
    user_message,
    user_id=None
):
    """
    Create an idempotent request and its user message atomically.

    If the same request ID and content already exist, return the
    existing request without inserting another user message.

    Returns a dictionary containing the request fields and a
    'created' flag indicating whether a new request was inserted.
    """

    _validate_required_text(request_id, "request_id")
    _validate_required_text(conversation_id, "conversation_id")
    _validate_required_text(user_message, "user_message")
    _validate_user_id(user_id)

    request_hash = _calculate_request_hash(
        conversation_id,
        user_message
    )

    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        # Serialize competing requests before checking the request ID.
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
                "Cannot create a request for a closed conversation."
            )

        # Check whether this request ID has already been processed.
        cursor.execute(
            """
            SELECT *
            FROM conversation_requests
            WHERE request_id = ?;
            """,
            (request_id,)
        )

        existing_request = _row_to_dict(
            cursor,
            cursor.fetchone()
        )

        if existing_request is not None:

            if (
                existing_request["conversation_id"] != conversation_id
                or existing_request["request_hash"] != request_hash
            ):
                raise ValueError(
                    "Request ID has already been used with different "
                    "conversation or message content."
                )

            connection.commit()

            existing_request["created"] = False
            return existing_request

        current_time = _current_timestamp()

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

        # Store the user message within this same transaction.
        cursor.execute(
            """
            INSERT INTO conversation_messages (
                conversation_id,
                sequence_number,
                role,
                content,
                created_at
            )
            VALUES (?, ?, 'user', ?, ?);
            """,
            (
                conversation_id,
                sequence_number,
                user_message,
                current_time
            )
        )

        user_message_id = cursor.lastrowid

        # Create the corresponding idempotency record.
        cursor.execute(
            """
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
            VALUES (?, ?, ?, NULL, ?, 'processing', NULL, ?, ?, NULL);
            """,
            (
                request_id,
                conversation_id,
                user_message_id,
                request_hash,
                current_time,
                current_time
            )
        )

        # Update the conversation timestamp.
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
            FROM conversation_requests
            WHERE request_id = ?;
            """,
            (request_id,)
        )

        request = _row_to_dict(cursor, cursor.fetchone())

        connection.commit()

        request["created"] = True
        return request

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


def get_request(request_id, user_id=None):
    """
    Retrieve a request only if its conversation belongs to the
    supplied registered user or guest context.
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
            (request_id,)
        )

        request = _row_to_dict(cursor, cursor.fetchone())

        if request is None:
            return None

        conversation = _get_owned_conversation(
            cursor,
            request["conversation_id"],
            user_id
        )

        if conversation is None:
            return None

        return request

    finally:
        connection.close()


def complete_request(
    request_id,
    response_message_id,
    user_id=None
):
    """
    Mark a processing request as completed.

    The response message must be an assistant message belonging
    to the same conversation as the request.
    """

    _validate_required_text(request_id, "request_id")
    _validate_user_id(user_id)

    if (
        isinstance(response_message_id, bool)
        or not isinstance(response_message_id, int)
        or response_message_id <= 0
    ):
        raise ValueError(
            "response_message_id must be a positive integer."
        )

    connection = get_db_connection()

    try:
        cursor = connection.cursor()
        cursor.execute("BEGIN IMMEDIATE;")

        cursor.execute(
            """
            SELECT *
            FROM conversation_requests
            WHERE request_id = ?;
            """,
            (request_id,)
        )

        request = _row_to_dict(cursor, cursor.fetchone())

        if request is None:
            raise ValueError("Request not found or access not permitted.")

        conversation = _get_owned_conversation(
            cursor,
            request["conversation_id"],
            user_id
        )

        if conversation is None:
            raise ValueError("Request not found or access not permitted.")

        if request["status"] == "completed":
            if request["response_message_id"] == response_message_id:
                connection.commit()
                return True

            raise ValueError(
                "Request has already been completed with another response."
            )

        if request["status"] != "processing":
            raise ValueError(
                "Only processing requests can be completed."
            )

        cursor.execute(
            """
            SELECT role
            FROM conversation_messages
            WHERE conversation_id = ?
              AND message_id = ?;
            """,
            (
                request["conversation_id"],
                response_message_id
            )
        )

        response = cursor.fetchone()

        if response is None or response[0] != "assistant":
            raise ValueError(
                "Response must be an assistant message in the same "
                "conversation."
            )

        current_time = _current_timestamp()

        cursor.execute(
            """
            UPDATE conversation_requests
            SET
                status = 'completed',
                response_message_id = ?,
                error_code = NULL,
                updated_at = ?,
                completed_at = ?
            WHERE request_id = ?
              AND status = 'processing';
            """,
            (
                response_message_id,
                current_time,
                current_time,
                request_id
            )
        )

        if cursor.rowcount != 1:
            raise ValueError("Request status could not be updated.")

        connection.commit()
        return True

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


def _set_request_status(
    request_id,
    new_status,
    user_id=None,
    error_code=None
):
    """Safely move a processing request to a failure state."""

    _validate_required_text(request_id, "request_id")
    _validate_user_id(user_id)

    if new_status not in {"failed", "interrupted"}:
        raise ValueError(
            "new_status must be 'failed' or 'interrupted'."
        )

    if error_code is not None:
        if not isinstance(error_code, str) or not error_code.strip():
            raise ValueError(
                "error_code must be a non-empty string or None."
            )

    connection = get_db_connection()

    try:
        cursor = connection.cursor()
        cursor.execute("BEGIN IMMEDIATE;")

        cursor.execute(
            """
            SELECT *
            FROM conversation_requests
            WHERE request_id = ?;
            """,
            (request_id,)
        )

        request = _row_to_dict(cursor, cursor.fetchone())

        if request is None:
            raise ValueError("Request not found or access not permitted.")

        conversation = _get_owned_conversation(
            cursor,
            request["conversation_id"],
            user_id
        )

        if conversation is None:
            raise ValueError("Request not found or access not permitted.")

        if request["status"] == new_status:
            if request["error_code"] == error_code:
                connection.commit()
                return True

            raise ValueError(
                "Request already has this status with a different error code."
            )

        if request["status"] != "processing":
            raise ValueError(
                "Only processing requests can change to a failure state."
            )

        current_time = _current_timestamp()

        cursor.execute(
            """
            UPDATE conversation_requests
            SET
                status = ?,
                error_code = ?,
                updated_at = ?
            WHERE request_id = ?
              AND status = 'processing';
            """,
            (
                new_status,
                error_code,
                current_time,
                request_id
            )
        )

        if cursor.rowcount != 1:
            raise ValueError("Request status could not be updated.")

        connection.commit()
        return True

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


def fail_request(request_id, user_id=None, error_code=None):
    """Mark a processing request as failed."""
    return _set_request_status(
        request_id,
        "failed",
        user_id=user_id,
        error_code=error_code
    )


def interrupt_request(request_id, user_id=None, error_code=None):
    """Mark a processing request as interrupted."""
    return _set_request_status(
        request_id,
        "interrupted",
        user_id=user_id,
        error_code=error_code
    )


def list_pending_requests(
    conversation_id,
    user_id=None,
    limit=100
):
    """
    List processing and interrupted requests for an owned conversation.

    Failed and completed requests are excluded.
    """

    _validate_required_text(conversation_id, "conversation_id")
    _validate_user_id(user_id)

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

        cursor.execute(
            """
            SELECT *
            FROM conversation_requests
            WHERE conversation_id = ?
              AND status IN ('processing', 'interrupted')
            ORDER BY created_at ASC, request_id ASC
            LIMIT ?;
            """,
            (conversation_id, limit)
        )

        columns = [column[0] for column in cursor.description]

        return [
            dict(zip(columns, row))
            for row in cursor.fetchall()
        ]

    finally:
        connection.close()

