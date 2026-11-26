"""Security and payload validation for AfterShip Tracking webhooks."""

import base64
import binascii
import hashlib
import hmac
import json
import math
import uuid


SUPPORTED_EVENTS = {
    "tracking_update",
    "edd_revise",
    "tracking_pending_time",
}

SIGNATURE_HEADER = "aftership-hmac-sha256"


class WebhookValidationError(ValueError):
    """Raised when a webhook payload is invalid."""


def verify_aftership_signature(
    raw_body: bytes,
    signature_header: str,
    secret: str,
) -> bool:
    """
    Verify an AfterShip webhook HMAC-SHA256 signature.

    The signature is calculated over the original raw request bytes.
    The expected digest is Base64 encoded.
    """

    if not isinstance(raw_body, bytes):
        return False

    if not raw_body or not isinstance(secret, str) or not secret:
        return False

    if not isinstance(signature_header, str):
        return False

    supplied_signature = signature_header.strip()

    if not supplied_signature:
        return False

    try:
        supplied_digest = base64.b64decode(
            supplied_signature,
            validate=True,
        )
    except (ValueError, binascii.Error):
        return False

    if len(supplied_digest) != hashlib.sha256().digest_size:
        return False

    expected_digest = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).digest()

    return hmac.compare_digest(
        expected_digest,
        supplied_digest,
    )


def validate_aftership_payload(raw_body: bytes) -> dict:
    """
    Parse and validate the documented AfterShip webhook envelope.

    Returns the parsed dictionary when valid.

    This function deliberately does not require every tracking
    detail inside msg because event payloads can differ.
    """

    if not isinstance(raw_body, bytes):
        raise WebhookValidationError(
            "Webhook body must be bytes."
        )

    if not raw_body:
        raise WebhookValidationError(
            "Webhook body cannot be empty."
        )

    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise WebhookValidationError(
            "Webhook body must contain valid JSON."
        ) from None

    if not isinstance(payload, dict):
        raise WebhookValidationError(
            "Webhook JSON root must be an object."
        )

    required_fields = {
        "ts",
        "event",
        "event_id",
        "msg",
    }

    missing_fields = required_fields - payload.keys()

    if missing_fields:
        missing = ", ".join(sorted(missing_fields))

        raise WebhookValidationError(
            f"Missing required webhook fields: {missing}."
        )

    event_type = payload["event"]

    if not isinstance(event_type, str):
        raise WebhookValidationError(
            "Webhook event must be a string."
        )

    if event_type not in SUPPORTED_EVENTS:
        raise WebhookValidationError(
            "Unsupported webhook event type."
        )

    event_id = payload["event_id"]

    if not isinstance(event_id, str):
        raise WebhookValidationError(
            "Webhook event_id must be a UUID v4 string."
        )

    try:
        parsed_event_id = uuid.UUID(event_id)
    except (ValueError, AttributeError):
        raise WebhookValidationError(
            "Webhook event_id must be a valid UUID v4."
        ) from None

    if parsed_event_id.version != 4:
        raise WebhookValidationError(
            "Webhook event_id must be a UUID v4."
        )

    timestamp = payload["ts"]

    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not math.isfinite(timestamp)
        or timestamp <= 0
    ):
        raise WebhookValidationError(
            "Webhook ts must be a positive UTC Unix timestamp."
        )

    message = payload["msg"]

    if not isinstance(message, dict):
        raise WebhookValidationError(
            "Webhook msg must be an object."
        )

    return payload