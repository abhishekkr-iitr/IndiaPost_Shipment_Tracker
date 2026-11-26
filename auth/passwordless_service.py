"""Passwordless email authentication service for registered users.

Issues short-lived, single-use authentication challenges (storing only SHA-256 digests),
enforces attempt/rate limits, and issues secure opaque session tokens tied to
server-derived canonical user_ids.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from database.auth_repository import (
    create_auth_challenge,
    get_auth_challenge,
    record_challenge_attempt,
    consume_auth_challenge,
    get_recent_active_challenge_count,
    get_or_create_user_by_email,
    create_auth_session,
    get_auth_session,
    revoke_auth_session,
    normalize_email,
)


class AuthenticationError(Exception):
    """Raised when challenge verification or authentication fails."""


class RateLimitExceededError(AuthenticationError):
    """Raised when too many challenges are requested in a short window."""


class EmailDeliveryProvider(Protocol):
    """Protocol for sending email authentication challenges."""

    def send_challenge(self, email: str, code: str, expires_in_seconds: int) -> bool:
        """Send an authentication challenge code to the recipient."""
        ...


class MockEmailDeliveryProvider:
    """In-memory mock delivery provider for tests and local development."""

    def __init__(self) -> None:
        self.sent_challenges: list[dict[str, Any]] = []

    def send_challenge(self, email: str, code: str, expires_in_seconds: int) -> bool:
        self.sent_challenges.append({
            "email": email,
            "code": code,
            "expires_in_seconds": expires_in_seconds,
            "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        return True

    def clear(self) -> None:
        self.sent_challenges.clear()

    @property
    def latest_code(self) -> str | None:
        return self.sent_challenges[-1]["code"] if self.sent_challenges else None


class PasswordlessAuthService:
    """Manages passwordless challenges, verification, and session lifecycle."""

    def __init__(
        self,
        email_provider: EmailDeliveryProvider | None = None,
        *,
        challenge_ttl_seconds: int = 600,
        session_ttl_seconds: int = 30 * 24 * 3600,
        max_attempts: int = 3,
        rate_limit_max_active: int = 3,
    ) -> None:
        self.email_provider = email_provider or MockEmailDeliveryProvider()
        self.challenge_ttl_seconds = challenge_ttl_seconds
        self.session_ttl_seconds = session_ttl_seconds
        self.max_attempts = max_attempts
        self.rate_limit_max_active = rate_limit_max_active

    @staticmethod
    def _hash_secret(secret: str) -> str:
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()

    def request_login_challenge(self, email: str) -> dict[str, Any]:
        """Request a passwordless login challenge for an email address.

        Generates a 6-digit numeric code, hashes it, saves the challenge,
        and dispatches it via the configured provider.
        """
        normalized = normalize_email(email)

        # Rate limiting: check active pending challenges
        active_count = get_recent_active_challenge_count(normalized, identity_type="email")
        if active_count >= self.rate_limit_max_active:
            raise RateLimitExceededError(
                "Too many pending authentication requests. Please wait or check your inbox."
            )

        # Generate 6-digit cryptographic numeric OTP
        code = "".join(secrets.choice("0123456789") for _ in range(6))
        code_hash = self._hash_secret(code)

        now_dt = datetime.now(timezone.utc)
        expires_dt = now_dt + timedelta(seconds=self.challenge_ttl_seconds)
        expires_at = expires_dt.isoformat(timespec="seconds")

        challenge_id = create_auth_challenge(
            identifier=normalized,
            challenge_hash=code_hash,
            expires_at=expires_at,
            identity_type="email",
            max_attempts=self.max_attempts,
        )

        # Dispatch via email delivery provider
        self.email_provider.send_challenge(
            email=normalized,
            code=code,
            expires_in_seconds=self.challenge_ttl_seconds,
        )

        return {
            "challenge_id": challenge_id,
            "expires_at": expires_at,
            "identifier": normalized,
        }

    def verify_challenge_and_create_session(
        self,
        challenge_id: str,
        code: str,
    ) -> dict[str, Any]:
        """Verify the challenge code, consume it, and issue an authenticated session.

        Derives canonical user_id from trusted server-side context.
        """
        if not isinstance(code, str) or not code.strip():
            raise AuthenticationError("Verification code is required.")

        challenge = get_auth_challenge(challenge_id)
        if challenge is None:
            raise AuthenticationError("Invalid or expired authentication challenge.")

        if challenge["consumed_at"] is not None:
            raise AuthenticationError("This challenge has already been used.")

        now_str = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if challenge["expires_at"] <= now_str:
            raise AuthenticationError("This challenge has expired.")

        if challenge["attempt_count"] >= challenge["max_attempts"]:
            raise AuthenticationError("Maximum verification attempts exceeded.")

        # Constant-time comparison of SHA-256 hashes
        candidate_hash = self._hash_secret(code.strip())
        is_match = hmac.compare_digest(candidate_hash, challenge["challenge_hash"])

        if not is_match:
            new_attempts = record_challenge_attempt(challenge_id)
            remaining = max(0, challenge["max_attempts"] - new_attempts)
            if remaining == 0:
                raise AuthenticationError("Maximum attempts reached. Challenge invalidated.")
            raise AuthenticationError(f"Incorrect verification code. {remaining} attempt(s) remaining.")

        # Consume challenge atomically
        consumed = consume_auth_challenge(challenge_id)
        if not consumed:
            raise AuthenticationError("Challenge could not be consumed (expired or concurrent use).")

        # Derive canonical user_id
        user_id = get_or_create_user_by_email(challenge["identifier"])

        # Generate secure bearer session token
        bearer_token = secrets.token_urlsafe(32)
        token_hash = self._hash_secret(bearer_token)

        now_dt = datetime.now(timezone.utc)
        session_expires_dt = now_dt + timedelta(seconds=self.session_ttl_seconds)
        session_expires_at = session_expires_dt.isoformat(timespec="seconds")

        create_auth_session(
            token_hash=token_hash,
            user_id=user_id,
            expires_at=session_expires_at,
        )

        return {
            "token": bearer_token,
            "user_id": user_id,
            "email": challenge["identifier"],
            "expires_at": session_expires_at,
        }

    def authenticate_session(self, token: str) -> dict[str, Any] | None:
        """Validate an opaque bearer session token and return trusted session dict."""
        if not isinstance(token, str) or not token.strip():
            return None

        token_hash = self._hash_secret(token.strip())
        return get_auth_session(token_hash)

    def logout_session(self, token: str) -> bool:
        """Revoke a bearer session token."""
        if not isinstance(token, str) or not token.strip():
            return False

        token_hash = self._hash_secret(token.strip())
        return revoke_auth_session(token_hash)
