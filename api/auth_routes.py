"""FastAPI router for passwordless email authentication.

Provides challenge request, verification, session management, and profile
retrieval for registered users. Session tokens are transmitted via HttpOnly
cookies and/or Authorization Bearer headers.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Cookie, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from auth.passwordless_service import (
    AuthenticationError,
    PasswordlessAuthService,
    RateLimitExceededError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["authentication"])

AUTH_SESSION_COOKIE = "india_post_auth_session"
AUTH_COOKIE_MAX_AGE_SECONDS = 30 * 24 * 3600  # 30 days

# Singleton default service instance (can be overridden in tests or app startup)
_auth_service: PasswordlessAuthService | None = None


def get_auth_service() -> PasswordlessAuthService:
    global _auth_service
    if _auth_service is None:
        _auth_service = PasswordlessAuthService()
    return _auth_service


def set_auth_service(service: PasswordlessAuthService | None) -> None:
    global _auth_service
    _auth_service = service


class ChallengeRequest(BaseModel):
    email: str = Field(min_length=3, max_length=256)


class ChallengeResponse(BaseModel):
    success: bool = True
    challenge_id: str
    expires_at: str
    message: str = "Verification code sent to email."


class VerifyRequest(BaseModel):
    challenge_id: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=6, max_length=8)


class VerifyResponse(BaseModel):
    success: bool = True
    token: str
    user_id: int
    email: str
    expires_at: str


class AuthProfileResponse(BaseModel):
    authenticated: bool = True
    user_id: int
    expires_at: str


class LogoutResponse(BaseModel):
    success: bool = True
    message: str = "Logged out successfully."


def _extract_token(
    authorization: str | None,
    cookie_token: str | None,
) -> str | None:
    """Extract session token from Bearer header or fallback to cookie."""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token:
            return token
    if cookie_token and cookie_token.strip():
        return cookie_token.strip()
    return None


@router.post("/challenge", response_model=ChallengeResponse)
def request_challenge(payload: ChallengeRequest) -> ChallengeResponse:
    """Request a single-use login code sent to the provided email."""
    service = get_auth_service()
    try:
        result = service.request_login_challenge(payload.email)
        return ChallengeResponse(
            challenge_id=result["challenge_id"],
            expires_at=result["expires_at"],
        )
    except RateLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        )
    except Exception as exc:
        logger.warning("Authentication challenge request failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not issue authentication challenge.",
        )


@router.post("/verify", response_model=VerifyResponse)
def verify_challenge(
    payload: VerifyRequest,
    response: Response,
) -> VerifyResponse:
    """Verify challenge code and establish an authenticated session."""
    service = get_auth_service()
    try:
        result = service.verify_challenge_and_create_session(
            challenge_id=payload.challenge_id,
            code=payload.code,
        )
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        )

    # Set secure HttpOnly session cookie
    response.set_cookie(
        key=AUTH_SESSION_COOKIE,
        value=result["token"],
        max_age=AUTH_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
    )

    return VerifyResponse(
        token=result["token"],
        user_id=result["user_id"],
        email=result["email"],
        expires_at=result["expires_at"],
    )


@router.get("/me", response_model=AuthProfileResponse)
def get_current_user_profile(
    authorization: str | None = Header(default=None),
    session_cookie: str | None = Cookie(default=None, alias=AUTH_SESSION_COOKIE),
) -> AuthProfileResponse:
    """Retrieve profile of the currently authenticated registered user."""
    token = _extract_token(authorization, session_cookie)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )

    service = get_auth_service()
    session = service.authenticate_session(token)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session is invalid or expired.",
        )

    return AuthProfileResponse(
        user_id=session["user_id"],
        expires_at=session["expires_at"],
    )


@router.post("/logout", response_model=LogoutResponse)
def logout(
    response: Response,
    authorization: str | None = Header(default=None),
    session_cookie: str | None = Cookie(default=None, alias=AUTH_SESSION_COOKIE),
) -> LogoutResponse:
    """Revoke the current authentication session."""
    token = _extract_token(authorization, session_cookie)
    if token:
        service = get_auth_service()
        service.logout_session(token)

    response.delete_cookie(
        key=AUTH_SESSION_COOKIE,
        httponly=True,
        samesite="lax",
    )
    return LogoutResponse()
