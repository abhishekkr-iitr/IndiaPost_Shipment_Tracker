"""FastAPI HTTP receiver for AfterShip webhook events."""

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from database.webhook_event_repository import save_webhook_event
from webhooks.webhook_config import load_webhook_config
from webhooks.webhook_security import (
    SIGNATURE_HEADER,
    WebhookValidationError,
    validate_aftership_payload,
    verify_aftership_signature,
)


def create_app(config=None):
    """
    Create the AfterShip webhook FastAPI application.

    Configuration can be injected for isolated testing.
    Production configuration is loaded from environment variables.
    """
    if config is None:
        config = load_webhook_config()

    app = FastAPI(
        title="India Post AI Shipment Assistant - Webhook Receiver",
        docs_url=None,
        redoc_url=None,
    )

    @app.post("/webhooks/aftership")
    async def receive_aftership_webhook(request: Request):
        """Validate and durably accept an AfterShip webhook."""
        if not config.enabled:
            raise HTTPException(
                status_code=503,
                detail="Webhook reception is disabled.",
            )

        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid Content-Length header.",
                ) from None

            if declared_length < 0:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid Content-Length header.",
                )

            if declared_length > config.max_body_bytes:
                raise HTTPException(
                    status_code=413,
                    detail="Webhook request body is too large.",
                )

        # Preserve the exact bytes used for signature verification and
        # enforce the body limit while streaming the request.
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > config.max_body_bytes:
                raise HTTPException(
                    status_code=413,
                    detail="Webhook request body is too large.",
                )
            body.extend(chunk)

        raw_body = bytes(body)
        if not raw_body:
            raise HTTPException(
                status_code=400,
                detail="Webhook request body is empty.",
            )

        signature = request.headers.get(SIGNATURE_HEADER)
        if not verify_aftership_signature(
            raw_body,
            signature,
            config.secret,
        ):
            raise HTTPException(
                status_code=401,
                detail="Invalid webhook signature.",
            )

        try:
            payload = validate_aftership_payload(raw_body)
        except WebhookValidationError:
            raise HTTPException(
                status_code=400,
                detail="Invalid webhook payload.",
            ) from None

        # Persist durably before acknowledging; processing is performed
        # separately by the background worker.
        try:
            result = save_webhook_event(
                payload,
                raw_body,
                provider="aftership",
            )
        except Exception:
            # Do not expose database errors or internal details.
            # A non-2xx response allows the provider to retry.
            raise HTTPException(
                status_code=503,
                detail="Unable to durably accept webhook event.",
            ) from None

        if result["inserted"]:
            return JSONResponse(
                status_code=202,
                content={"accepted": True, "duplicate": False},
            )

        return JSONResponse(
            status_code=200,
            content={"accepted": True, "duplicate": True},
        )

    return app
