"""Same-origin conversation API for the local browser application.

Guest identity is held server-side in SQLite behind an opaque random cookie.
Only a SHA-256 digest of the cookie token is persisted; the browser never
chooses trusted user context or gains access to conversations outside its
session association. Conversation history survives application restarts.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ai_tools.openai_agent import OpenAIAgentError, ask_openai
from database.conversation_repository import (
    close_conversation,
    create_conversation,
    get_conversation,
    get_conversation_messages,
)
from api.browser_session_store import (
    add_conversation,
    create_session as persist_session,
    get_session as load_session,
    is_owned_conversation,
    list_conversations as load_conversations,
    set_active_conversation,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["local-conversation"])

SESSION_COOKIE = "india_post_local_session"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
MAX_MESSAGE_LENGTH = 4000

# Session tokens are opaque browser cookies; only SHA-256 digests are persisted.


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)
    request_id: str = Field(min_length=1, max_length=64)


class MessageResponse(BaseModel):
    conversation_id: str
    request_id: str
    response: str


def _safe_exception_metadata(exc: BaseException) -> dict:
    """Extract bounded, non-secret diagnostic fields without logging messages."""
    metadata = {"cause_type": type(exc).__name__}

    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and not isinstance(status_code, bool):
        metadata["status_code"] = status_code

    for attribute, output_name in (
        ("code", "api_error_code"),
        ("type", "api_error_type"),
        ("request_id", "api_request_id"),
    ):
        value = getattr(exc, attribute, None)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            value = str(value).strip()
            if value:
                metadata[output_name] = value[:160]

    # Some SDK errors expose the request ID through response headers instead.
    if "api_request_id" not in metadata:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if headers is not None:
            try:
                request_id = headers.get("x-request-id")
            except Exception:
                request_id = None
            if isinstance(request_id, str) and request_id.strip():
                metadata["api_request_id"] = request_id.strip()[:160]

    return metadata


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_session() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    conversation_id = create_conversation(channel="local_web")
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=SESSION_MAX_AGE_SECONDS)).isoformat(timespec="seconds")
    digest = _token_hash(token)
    persist_session(digest, expires_at)
    add_conversation(digest, conversation_id, make_active=True)
    return token, conversation_id


def _session_for(token: str | None) -> tuple[str, str, bool]:
    if token:
        session = load_session(_token_hash(token))
        if session:
            conversation_id = session.get("active_conversation_id")
            if conversation_id:
                return token, conversation_id, False
            conversation_id = create_conversation(channel="local_web")
            add_conversation(_token_hash(token), conversation_id, make_active=True)
            return token, conversation_id, False
    new_token, new_conversation_id = _new_session()
    return new_token, new_conversation_id, True


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=False,  # Localhost HTTP only; do not expose this app on a LAN.
        samesite="strict",
        path="/",
    )


def _require_same_origin(request: Request) -> None:
    """Reject explicit cross-origin writes; absent Origin supports local tools."""
    origin = request.headers.get("origin")
    if origin:
        expected = f"{request.url.scheme}://{request.url.netloc}"
        if origin.rstrip("/") != expected.rstrip("/"):
            raise HTTPException(status_code=403, detail="Cross-origin request rejected.")

    fetch_site = request.headers.get("sec-fetch-site", "").lower()
    if fetch_site in {"cross-site", "same-site"}:
        # The app is intended to be a standalone localhost origin.
        raise HTTPException(status_code=403, detail="Cross-site request rejected.")


def _get_active_session(token: str | None) -> tuple[str, str]:
    if not token:
        raise HTTPException(status_code=401, detail="Local session is missing. Reload the application.")
    session = load_session(_token_hash(token))
    if not session:
        raise HTTPException(status_code=401, detail="Local session has expired. Reload the application.")
    conversation_id = session.get("active_conversation_id")
    if not conversation_id or not is_owned_conversation(_token_hash(token), conversation_id):
        raise HTTPException(status_code=409, detail="No active conversation. Start a new conversation.")
    conversation = get_conversation(conversation_id)
    if not conversation or conversation.get("status") != "active":
        raise HTTPException(status_code=409, detail="This conversation is closed. Start a new conversation.")
    return _token_hash(token), conversation_id


def _get_session_hash(token: str | None) -> str:
    if not token:
        raise HTTPException(status_code=401, detail="Local session is missing. Reload the application.")
    digest = _token_hash(token)
    if not load_session(digest):
        raise HTTPException(status_code=401, detail="Local session has expired. Reload the application.")
    return digest


def _public_messages(conversation_id: str) -> list[dict]:
    messages = get_conversation_messages(conversation_id)
    # Do not expose system prompts, tool payloads, tool-call metadata or provider IDs.
    return [
        {"role": item["role"], "content": item["content"]}
        for item in messages
        if item.get("role") in {"user", "assistant"}
    ]


@router.get("/session")
def get_or_create_session(
    response: Response,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
):
    token, conversation_id, created = _session_for(session_token)
    if created:
        _set_session_cookie(response, token)
    return {
        "conversation_id": conversation_id,
        "messages": _public_messages(conversation_id),
        "conversations": load_conversations(_token_hash(token)),
    }


@router.get("/conversations")
def get_conversations(
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
):
    return {"conversations": load_conversations(_get_session_hash(session_token))}


@router.post("/conversations")
def new_conversation(
    request: Request,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
):
    _require_same_origin(request)
    digest = _get_session_hash(session_token)
    conversation_id = create_conversation(channel="local_web")
    add_conversation(digest, conversation_id, make_active=True)
    return {"conversation_id": conversation_id, "messages": [], "status": "active"}


@router.get("/conversations/{conversation_id}")
def open_conversation(
    conversation_id: str,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
):
    digest = _get_session_hash(session_token)
    selected = set_active_conversation(digest, conversation_id)
    if selected is None:
        raise HTTPException(status_code=404, detail="Conversation not found in this browser session.")
    return {
        "conversation_id": conversation_id,
        "status": selected["status"],
        "messages": _public_messages(conversation_id),
    }


@router.post("/conversations/{conversation_id}/close")
def close_browser_conversation(
    conversation_id: str,
    request: Request,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
):
    _require_same_origin(request)
    digest = _get_session_hash(session_token)
    if not is_owned_conversation(digest, conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found in this browser session.")
    if not close_conversation(conversation_id):
        raise HTTPException(status_code=409, detail="Conversation is already closed.")
    replacement_id = create_conversation(channel="local_web")
    add_conversation(digest, replacement_id, make_active=True)
    return {"conversation_id": replacement_id, "closed_conversation_id": conversation_id, "messages": []}


@router.get("/messages")
def get_messages(
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
):
    _, conversation_id = _get_active_session(session_token)
    return {"conversation_id": conversation_id, "messages": _public_messages(conversation_id)}


@router.post("/messages", response_model=MessageResponse)
def send_message(
    payload: MessageRequest,
    request: Request,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
):
    _require_same_origin(request)
    _, conversation_id = _get_active_session(session_token)

    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="Message cannot be blank.")

    try:
        # UUID validation ensures request IDs have a predictable bounded format.
        request_id = str(UUID(payload.request_id))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail="request_id must be a UUID.")

    try:
        answer = ask_openai(
            message,
            conversation_id=conversation_id,
            request_id=request_id,
            user_context=None,  # Guest identity is established by the server session.
        )
    except OpenAIAgentError as exc:
        cause = exc.__cause__
        metadata = (
            _safe_exception_metadata(cause)
            if cause is not None
            else {"cause_type": "not_preserved"}
        )
        logger.warning(
            "Local assistant request failed: agent_error_type=%s cause_type=%s "
            "status_code=%s api_error_code=%s api_error_type=%s api_request_id=%s",
            type(exc).__name__,
            metadata.get("cause_type", "unknown"),
            metadata.get("status_code", "unavailable"),
            metadata.get("api_error_code", "unavailable"),
            metadata.get("api_error_type", "unavailable"),
            metadata.get("api_request_id", "unavailable"),
        )
        raise HTTPException(
            status_code=502,
            detail="The assistant could not complete this request. Check the local configuration and try again.",
        )
    except ValueError as exc:
        logger.info("Local assistant rejected request: %s", type(exc).__name__)
        raise HTTPException(status_code=422, detail="The request could not be validated.")
    except Exception:
        logger.exception("Unexpected local assistant request failure.")
        raise HTTPException(status_code=500, detail="An unexpected application error occurred.")

    return MessageResponse(
        conversation_id=conversation_id,
        request_id=request_id,
        response=answer,
    )
