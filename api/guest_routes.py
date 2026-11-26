"""FastAPI router for durable guest notification management.

Manages durable guest identities, session cookies, shipment tracking
subscriptions, channel preferences, consent, and Telegram deep linking.
All guest state is held server-side in SQLite and referenced by opaque cookie.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Cookie, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from database.guest_notification_repository import (
    create_guest_notification_identity,
    create_guest_session,
    get_guest_session,
    create_guest_shipment_subscription,
    get_guest_subscription_by_id,
    list_guest_subscriptions,
    update_guest_monitoring_status,
    save_guest_channel_preference,
    get_guest_channel_preference,
    revoke_guest_channel_consent,
)
from database.shipment_repository import (
    get_shipment_by_tracking_number,
    save_shipment,
)
from notifications.telegram_linking import create_telegram_link_challenge

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/guest", tags=["guest-notifications"])

GUEST_SESSION_COOKIE = "india_post_guest_session"
GUEST_COOKIE_MAX_AGE_SECONDS = 30 * 24 * 3600  # 30 days


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _resolve_or_create_guest(
    request: Request,
    response: Response,
    session_cookie: str | None,
) -> tuple[str, str]:
    """Retrieve existing guest_identity_id or create a new durable guest identity.

    Returns (guest_identity_id, session_token).
    """
    if session_cookie and session_cookie.strip():
        token = session_cookie.strip()
        session = get_guest_session(_hash_token(token))
        if session:
            return session["guest_identity_id"], token

    # Create new identity and session
    guest_identity_id = create_guest_notification_identity(notes="Created via /api/guest")
    new_token = secrets.token_urlsafe(32)
    token_hash = _hash_token(new_token)

    now_dt = datetime.now(timezone.utc)
    expires_dt = now_dt + timedelta(seconds=GUEST_COOKIE_MAX_AGE_SECONDS)
    expires_at = expires_dt.isoformat(timespec="seconds")

    create_guest_session(token_hash, guest_identity_id, expires_at)

    response.set_cookie(
        key=GUEST_SESSION_COOKIE,
        value=new_token,
        max_age=GUEST_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return guest_identity_id, new_token


class GuestSubscribeRequest(BaseModel):
    tracking_number: str = Field(min_length=1, max_length=64)
    carrier: str = Field(default="india-post", max_length=64)
    channel: str = Field(pattern="^(telegram|email)$")
    recipient: str | None = Field(default=None, max_length=256)
    consent: bool = True
    monitoring_interval_hours: int = Field(default=6, ge=1, le=72)


class GuestSubscribeResponse(BaseModel):
    success: bool = True
    guest_identity_id: str
    guest_subscription_id: int
    shipment_id: int
    tracking_number: str
    requires_telegram_link: bool = False
    telegram_deep_link: str | None = None


class GuestUnsubscribeRequest(BaseModel):
    guest_subscription_id: int = Field(gt=0)


class GuestUnsubscribeResponse(BaseModel):
    success: bool = True
    guest_subscription_id: int
    monitoring_enabled: bool = False


class GuestTelegramLinkResponse(BaseModel):
    challenge_id: str
    link_code: str
    deep_link: str
    expires_at: str


@router.post("/subscribe", response_model=GuestSubscribeResponse)
def guest_subscribe(
    payload: GuestSubscribeRequest,
    request: Request,
    response: Response,
    session_cookie: str | None = Cookie(default=None, alias=GUEST_SESSION_COOKIE),
) -> GuestSubscribeResponse:
    """Subscribe a guest to shipment tracking notifications."""
    if not payload.consent:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Explicit consent is required to receive notifications.",
        )

    clean_tn = payload.tracking_number.strip()
    clean_carrier = payload.carrier.strip()

    # Resolve or create shipment record
    shipment = get_shipment_by_tracking_number(clean_tn, clean_carrier)
    if shipment is None:
        shipment_id = save_shipment({
            "tracking_number": clean_tn,
            "carrier": clean_carrier,
            "current_status": "InfoReceived",
            "provider": "local_manual",
        })
    else:
        shipment_id = shipment["shipment_id"]

    guest_id, _ = _resolve_or_create_guest(request, response, session_cookie)

    # Create shipment subscription
    sub_id = create_guest_shipment_subscription(
        guest_identity_id=guest_id,
        shipment_id=shipment_id,
        monitoring_interval_hours=payload.monitoring_interval_hours,
    )

    requires_telegram_link = False
    deep_link = None
    now_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if payload.channel == "email":
        if not payload.recipient or "@" not in payload.recipient:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A valid email recipient is required for email notifications.",
            )
        save_guest_channel_preference(
            guest_identity_id=guest_id,
            channel="email",
            recipient=payload.recipient.strip().lower(),
            enabled=True,
            verified_at=now_ts,
            consent_at=now_ts,
        )
    elif payload.channel == "telegram":
        # Check if guest already has verified telegram preference
        pref = get_guest_channel_preference(guest_id, "telegram")
        if not pref or not pref.get("recipient"):
            # Generate linking challenge
            challenge = create_telegram_link_challenge("guest", guest_id)
            requires_telegram_link = True
            deep_link = challenge["deep_link"]
        else:
            # Re-enable if previously disabled
            save_guest_channel_preference(
                guest_identity_id=guest_id,
                channel="telegram",
                recipient=pref["recipient"],
                enabled=True,
                verified_at=pref.get("verified_at"),
                consent_at=now_ts,
            )

    return GuestSubscribeResponse(
        guest_identity_id=guest_id,
        guest_subscription_id=sub_id,
        shipment_id=shipment_id,
        tracking_number=clean_tn,
        requires_telegram_link=requires_telegram_link,
        telegram_deep_link=deep_link,
    )


@router.get("/subscriptions")
def list_subscriptions(
    request: Request,
    response: Response,
    session_cookie: str | None = Cookie(default=None, alias=GUEST_SESSION_COOKIE),
) -> dict[str, Any]:
    """Retrieve all shipment subscriptions belonging to this guest session."""
    if not session_cookie or not session_cookie.strip():
        return {"guest_identity_id": None, "subscriptions": []}

    session = get_guest_session(_hash_token(session_cookie.strip()))
    if not session:
        return {"guest_identity_id": None, "subscriptions": []}

    guest_id = session["guest_identity_id"]
    subs = list_guest_subscriptions(guest_id)
    return {
        "guest_identity_id": guest_id,
        "subscriptions": subs,
    }


@router.post("/unsubscribe", response_model=GuestUnsubscribeResponse)
def guest_unsubscribe(
    payload: GuestUnsubscribeRequest,
    session_cookie: str | None = Cookie(default=None, alias=GUEST_SESSION_COOKIE),
) -> GuestUnsubscribeResponse:
    """Disable monitoring for a guest subscription."""
    if not session_cookie or not session_cookie.strip():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Guest session required.")

    session = get_guest_session(_hash_token(session_cookie.strip()))
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid guest session.")

    guest_id = session["guest_identity_id"]
    sub = get_guest_subscription_by_id(payload.guest_subscription_id)
    if sub is None or sub["guest_identity_id"] != guest_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found.")

    update_guest_monitoring_status(payload.guest_subscription_id, False)
    return GuestUnsubscribeResponse(
        guest_subscription_id=payload.guest_subscription_id,
        monitoring_enabled=False,
    )


@router.post("/telegram-link", response_model=GuestTelegramLinkResponse)
def get_telegram_link(
    request: Request,
    response: Response,
    session_cookie: str | None = Cookie(default=None, alias=GUEST_SESSION_COOKIE),
) -> GuestTelegramLinkResponse:
    """Generate a Telegram linking deep link challenge for the current guest identity."""
    guest_id, _ = _resolve_or_create_guest(request, response, session_cookie)
    challenge = create_telegram_link_challenge("guest", guest_id)

    return GuestTelegramLinkResponse(
        challenge_id=challenge["challenge_id"],
        link_code=challenge["link_code"],
        deep_link=challenge["deep_link"],
        expires_at=challenge["expires_at"],
    )
