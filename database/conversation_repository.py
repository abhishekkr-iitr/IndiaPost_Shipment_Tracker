
from datetime import datetime
from uuid import uuid4

from database.db_connection import get_db_connection


VALID_ROLES = {"user", "assistant", "tool", "system"}

_UNSET = object()

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


def create_conversation(
    channel,
    user_id=None,
    channel_conversation_id=None,
    provider=None,
    provider_conversation_id=None
):
    """Create a new registered-user or guest conversation."""

    _validate_user_id(user_id)

    if not isinstance(channel, str) or not channel.strip():
        raise ValueError("A valid channel name is required.")

    if channel_conversation_id is not None:
        if not isinstance(channel_conversation_id, str):
            raise ValueError("channel_conversation_id must be a string or None.")

    if provider is not None:
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("provider must be a non-empty string or None.")

    if provider_conversation_id is not None:
        if not isinstance(provider_conversation_id, str):
            raise ValueError(
                "provider_conversation_id must be a string or None."
            )

    conversation_id = str(uuid4())
    current_time = _current_timestamp()

    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            INSERT INTO conversations (
                conversation_id,
                user_id,
                channel,
                channel_conversation_id,
                status,
                provider,
                provider_conversation_id,
                created_at,
                updated_at,
                closed_at
            )
            VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, NULL);
            """,
            (
                conversation_id,
                user_id,
                channel.strip(),
                channel_conversation_id,
                provider,
                provider_conversation_id,
                current_time,
                current_time
            )
        )

        connection.commit()

        return conversation_id

    finally:
        connection.close()


def get_conversation(conversation_id, user_id=None):
    """Retrieve a conversation after checking its ownership."""

    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise ValueError("A valid conversation_id is required.")

    _validate_user_id(user_id)

    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        return _get_owned_conversation(
            cursor,
            conversation_id,
            user_id
        )

    finally:
        connection.close()


def list_user_conversations(user_id, limit=50):
    """List conversations belonging to one registered user."""

    _validate_user_id(user_id)

    if user_id is None:
        raise ValueError("A registered user_id is required.")

    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer.")

    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            SELECT *
            FROM conversations
            WHERE user_id = ?
            ORDER BY updated_at DESC, created_at DESC
            LIMIT ?;
            """,
            (user_id, limit)
        )

        columns = [column[0] for column in cursor.description]

        return [
            dict(zip(columns, row))
            for row in cursor.fetchall()
        ]

    finally:
        connection.close()



def update_conversation(
    conversation_id,
    user_id=None,
    provider=_UNSET,
    provider_conversation_id=_UNSET
):
    """
    Update selected provider metadata fields.

    Omitted fields remain unchanged.
    Explicitly passing None clears a field.
    """

    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise ValueError("A valid conversation_id is required.")

    _validate_user_id(user_id)

    if provider is not _UNSET:
        if provider is not None:
            if not isinstance(provider, str) or not provider.strip():
                raise ValueError(
                    "provider must be a non-empty string or None."
                )

    if provider_conversation_id is not _UNSET:
        if provider_conversation_id is not None:
            if not isinstance(provider_conversation_id, str):
                raise ValueError(
                    "provider_conversation_id must be a string or None."
                )

    updates = {}

    if provider is not _UNSET:
        updates["provider"] = provider

    if provider_conversation_id is not _UNSET:
        updates["provider_conversation_id"] = provider_conversation_id

    if not updates:
        return False

    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        conversation = _get_owned_conversation(
            cursor,
            conversation_id,
            user_id
        )

        if conversation is None:
            return False

        if conversation["status"] != "active":
            return False

        assignments = [
            f"{column} = ?"
            for column in updates
        ]

        assignments.append("updated_at = ?")

        values = list(updates.values())
        values.append(_current_timestamp())
        values.append(conversation_id)

        cursor.execute(
            f"""
            UPDATE conversations
            SET {", ".join(assignments)}
            WHERE conversation_id = ?;
            """,
            values
        )

        connection.commit()

        return cursor.rowcount == 1

    finally:
        connection.close()



def close_conversation(conversation_id, user_id=None):
    """Close an owned conversation."""

    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise ValueError("A valid conversation_id is required.")

    _validate_user_id(user_id)

    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        conversation = _get_owned_conversation(
            cursor,
            conversation_id,
            user_id
        )

        if conversation is None or conversation["status"] == "closed":
            return False

        current_time = _current_timestamp()

        cursor.execute(
            """
            UPDATE conversations
            SET
                status = 'closed',
                updated_at = ?,
                closed_at = ?
            WHERE conversation_id = ?;
            """,
            (current_time, current_time, conversation_id)
        )

        connection.commit()

        return cursor.rowcount == 1

    finally:
        connection.close()


def add_message(
    conversation_id,
    role,
    content,
    user_id=None,
    tool_name=None,
    tool_call_id=None,
    provider_message_id=None
):
    """
    Add a message to an owned active conversation.

    Sequence allocation and insertion happen in one write transaction.
    """

    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise ValueError("A valid conversation_id is required.")

    _validate_user_id(user_id)

    if role not in VALID_ROLES:
        _UNSET = object()
        raise ValueError(
            "role must be user, assistant, tool, or system."
        )

    if not isinstance(content, str):
        raise ValueError("Message content must be a string.")

    optional_fields = {
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "provider_message_id": provider_message_id
    }

    for field_name, field_value in optional_fields.items():
        if field_value is not None and not isinstance(field_value, str):
            raise ValueError(
                f"{field_name} must be a string or None."
            )

    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        # Reserve the write transaction before calculating the sequence.
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
            raise ValueError("Cannot add messages to a closed conversation.")

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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                conversation_id,
                sequence_number,
                role,
                content,
                tool_name,
                tool_call_id,
                provider_message_id,
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

        message_id = cursor.lastrowid

        connection.commit()

        return {
            "message_id": message_id,
            "conversation_id": conversation_id,
            "sequence_number": sequence_number
        }

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


def get_conversation_messages(
    conversation_id,
    user_id=None,
    limit=None
):
    """Retrieve messages in their original sequence order."""

    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise ValueError("A valid conversation_id is required.")

    _validate_user_id(user_id)

    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer or None.")

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
            FROM conversation_messages
            WHERE conversation_id = ?
            ORDER BY sequence_number ASC
        """

        parameters = [conversation_id]

        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)

        query += ";"

        cursor.execute(query, parameters)

        columns = [column[0] for column in cursor.description]

        return [
            dict(zip(columns, row))
            for row in cursor.fetchall()
        ]

    finally:
        connection.close()
