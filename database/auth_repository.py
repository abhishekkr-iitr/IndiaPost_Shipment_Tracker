"""Repository for registered-user passwordless authentication and session management.

Supports short-lived challenges (storing SHA-256 digests), server-side session
records, and canonical user identity association.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from database.db_connection import get_db_connection


EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_email(email: str) -> str:
    """Normalize and validate an email address."""
    if not isinstance(email, str):
        raise ValueError("Email must be a string.")
    normalized = email.strip().lower()
    if not normalized or not EMAIL_REGEX.match(normalized):
        raise ValueError(f"Invalid email address: {email!r}")
    return normalized


# ---------------------------------------------------------------------------
# Auth Challenges
# ---------------------------------------------------------------------------


def create_auth_challenge(
    identifier: str,
    challenge_hash: str,
    expires_at: str,
    identity_type: str = "email",
    max_attempts: int = 3,
    challenge_id: str | None = None,
) -> str:
    """Create a new short-lived auth challenge.

    Stores only the SHA-256 hash of the challenge secret.
    """
    if identity_type == "email":
        identifier = normalize_email(identifier)
    else:
        raise ValueError(f"Unsupported identity type: {identity_type}")

    if not isinstance(challenge_hash, str) or len(challenge_hash) != 64:
        raise ValueError("challenge_hash must be a 64-character SHA-256 hex string.")
    if not isinstance(expires_at, str) or not expires_at.strip():
        raise ValueError("expires_at must be a non-empty ISO timestamp.")
    if not isinstance(max_attempts, int) or max_attempts <= 0:
        raise ValueError("max_attempts must be a positive integer.")

    cid = challenge_id or str(uuid.uuid4())
    now_ts = _now()

    connection = get_db_connection()
    try:
        connection.execute(
            """INSERT INTO auth_challenges (
                challenge_id, identity_type, identifier, challenge_hash,
                attempt_count, max_attempts, expires_at, created_at
            ) VALUES (?, ?, ?, ?, 0, ?, ?, ?)""",
            (cid, identity_type, identifier, challenge_hash, max_attempts, expires_at, now_ts),
        )
        connection.commit()
        return cid
    finally:
        connection.close()


def get_auth_challenge(challenge_id: str) -> dict[str, Any] | None:
    """Retrieve an auth challenge by ID."""
    if not isinstance(challenge_id, str) or not challenge_id.strip():
        raise ValueError("challenge_id must be a non-empty string.")

    connection = get_db_connection()
    try:
        row = connection.execute(
            """SELECT challenge_id, identity_type, identifier, challenge_hash,
                      attempt_count, max_attempts, expires_at, consumed_at, created_at
               FROM auth_challenges
               WHERE challenge_id = ?""",
            (challenge_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "challenge_id": row[0],
            "identity_type": row[1],
            "identifier": row[2],
            "challenge_hash": row[3],
            "attempt_count": row[4],
            "max_attempts": row[5],
            "expires_at": row[6],
            "consumed_at": row[7],
            "created_at": row[8],
        }
    finally:
        connection.close()


def record_challenge_attempt(challenge_id: str) -> int:
    """Increment attempt count for a challenge and return the new count."""
    if not isinstance(challenge_id, str) or not challenge_id.strip():
        raise ValueError("challenge_id must be a non-empty string.")

    connection = get_db_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """UPDATE auth_challenges
               SET attempt_count = attempt_count + 1
               WHERE challenge_id = ?""",
            (challenge_id,),
        )
        row = connection.execute(
            "SELECT attempt_count FROM auth_challenges WHERE challenge_id = ?",
            (challenge_id,),
        ).fetchone()
        if row is None:
            connection.rollback()
            raise ValueError(f"Challenge not found: {challenge_id}")
        count = row[0]
        connection.commit()
        return count
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def consume_auth_challenge(challenge_id: str) -> bool:
    """Atomically consume a valid challenge.

    Returns True if consumed; False if already consumed or expired.
    """
    if not isinstance(challenge_id, str) or not challenge_id.strip():
        raise ValueError("challenge_id must be a non-empty string.")

    now_ts = _now()
    connection = get_db_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """UPDATE auth_challenges
               SET consumed_at = ?
               WHERE challenge_id = ?
                 AND consumed_at IS NULL
                 AND expires_at > ?
                 AND attempt_count < max_attempts""",
            (now_ts, challenge_id, now_ts),
        )
        success = cursor.rowcount > 0
        connection.commit()
        return success
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def get_recent_active_challenge_count(identifier: str, identity_type: str = "email") -> int:
    """Count non-expired, non-consumed challenges for rate-limiting."""
    if identity_type == "email":
        identifier = normalize_email(identifier)

    now_ts = _now()
    connection = get_db_connection()
    try:
        row = connection.execute(
            """SELECT COUNT(*) FROM auth_challenges
               WHERE identifier = ?
                 AND identity_type = ?
                 AND consumed_at IS NULL
                 AND expires_at > ?""",
            (identifier, identity_type, now_ts),
        ).fetchone()
        return row[0] if row else 0
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# User Auth Identities
# ---------------------------------------------------------------------------


def get_user_auth_identity(identifier: str, identity_type: str = "email") -> dict[str, Any] | None:
    """Retrieve a registered user's auth identity."""
    if identity_type == "email":
        identifier = normalize_email(identifier)

    connection = get_db_connection()
    try:
        row = connection.execute(
            """SELECT auth_identity_id, user_id, identity_type, identifier,
                      verified_at, created_at, updated_at
               FROM user_auth_identities
               WHERE identity_type = ? AND identifier = ?""",
            (identity_type, identifier),
        ).fetchone()
        if row is None:
            return None
        return {
            "auth_identity_id": row[0],
            "user_id": row[1],
            "identity_type": row[2],
            "identifier": row[3],
            "verified_at": row[4],
            "created_at": row[5],
            "updated_at": row[6],
        }
    finally:
        connection.close()


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    """Retrieve a user by internal primary key."""
    if not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("user_id must be a positive integer.")

    connection = get_db_connection()
    try:
        row = connection.execute(
            "SELECT user_id, external_user_id, created_at FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "user_id": row[0],
            "external_user_id": row[1],
            "created_at": row[2],
        }
    finally:
        connection.close()


def get_or_create_user_by_email(email: str) -> int:
    """Derive canonical internal user_id for a verified email address.

    If an identity already exists, returns its user_id.
    Otherwise, creates a record in `users` and an association in `user_auth_identities`.
    """
    normalized = normalize_email(email)
    now_ts = _now()

    connection = get_db_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")

        # Check existing identity
        row = connection.execute(
            """SELECT user_id FROM user_auth_identities
               WHERE identity_type = 'email' AND identifier = ?""",
            (normalized,),
        ).fetchone()

        if row is not None:
            connection.commit()
            return row[0]

        # Create new user in users table
        external_user_id = f"email_{normalized}"
        cursor = connection.execute(
            "INSERT INTO users (external_user_id, created_at) VALUES (?, ?)",
            (external_user_id, now_ts),
        )
        user_id = cursor.lastrowid

        # Create identity mapping
        connection.execute(
            """INSERT INTO user_auth_identities (
                user_id, identity_type, identifier, verified_at, created_at, updated_at
            ) VALUES (?, 'email', ?, ?, ?, ?)""",
            (user_id, normalized, now_ts, now_ts, now_ts),
        )

        connection.commit()
        return user_id
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Auth Sessions
# ---------------------------------------------------------------------------


def create_auth_session(token_hash: str, user_id: int, expires_at: str) -> None:
    """Create a new registered-user server session."""
    if not isinstance(token_hash, str) or len(token_hash) != 64:
        raise ValueError("token_hash must be a 64-character SHA-256 hex string.")
    if not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("user_id must be a positive integer.")
    if not isinstance(expires_at, str) or not expires_at.strip():
        raise ValueError("expires_at must be a non-empty ISO timestamp.")

    now_ts = _now()
    connection = get_db_connection()
    try:
        connection.execute(
            """INSERT INTO auth_sessions (
                token_hash, user_id, created_at, last_active_at, expires_at, revoked_at
            ) VALUES (?, ?, ?, ?, ?, NULL)""",
            (token_hash, user_id, now_ts, now_ts, expires_at),
        )
        connection.commit()
    finally:
        connection.close()


def get_auth_session(token_hash: str) -> dict[str, Any] | None:
    """Validate a session token hash.

    Returns the session dict if valid, active, and not revoked; otherwise None.
    Updates `last_active_at` on successful lookup.
    """
    if not isinstance(token_hash, str) or len(token_hash) != 64:
        return None

    now_ts = _now()
    connection = get_db_connection()
    try:
        row = connection.execute(
            """SELECT token_hash, user_id, created_at, last_active_at, expires_at, revoked_at
               FROM auth_sessions
               WHERE token_hash = ?""",
            (token_hash,),
        ).fetchone()

        if row is None:
            return None

        # Check revocation and expiry
        revoked_at = row[5]
        expires_at = row[4]
        if revoked_at is not None or expires_at <= now_ts:
            return None

        # Update last_active_at
        connection.execute(
            "UPDATE auth_sessions SET last_active_at = ? WHERE token_hash = ?",
            (now_ts, token_hash),
        )
        connection.commit()

        return {
            "token_hash": row[0],
            "user_id": row[1],
            "created_at": row[2],
            "last_active_at": now_ts,
            "expires_at": expires_at,
            "revoked_at": None,
        }
    finally:
        connection.close()


def revoke_auth_session(token_hash: str) -> bool:
    """Revoke a specific session."""
    if not isinstance(token_hash, str) or len(token_hash) != 64:
        return False

    now_ts = _now()
    connection = get_db_connection()
    try:
        cursor = connection.execute(
            """UPDATE auth_sessions
               SET revoked_at = ?
               WHERE token_hash = ? AND revoked_at IS NULL""",
            (now_ts, token_hash),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def revoke_all_user_sessions(user_id: int) -> int:
    """Revoke all active sessions for a user."""
    if not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("user_id must be a positive integer.")

    now_ts = _now()
    connection = get_db_connection()
    try:
        cursor = connection.execute(
            """UPDATE auth_sessions
               SET revoked_at = ?
               WHERE user_id = ? AND revoked_at IS NULL""",
            (now_ts, user_id),
        )
        connection.commit()
        return cursor.rowcount
    finally:
        connection.close()
