"""Telegram delivery adapter using an injected transport.

No HTTP calls, bot-token handling, or live Telegram integration are included.
A later transport implementation can satisfy TelegramTransport without changing
notification generation or the delivery worker.
"""
from typing import Protocol
from notifications.delivery import DeliveryResult


class TelegramTransport(Protocol):
    def send_message(self, chat_id: str, text: str) -> dict:
        """Return a provider-shaped response with ok/result.message_id."""


class TelegramAdapter:
    channel = "telegram"

    def __init__(self, transport: TelegramTransport):
        if not callable(getattr(transport, "send_message", None)):
            raise TypeError("Telegram transport must implement send_message().")
        self._transport = transport

    def deliver(self, notification: dict) -> DeliveryResult:
        if not isinstance(notification, dict):
            return DeliveryResult("failed", error_code="invalid_notification",
                                  error_message="Notification must be an object.")
        if notification.get("notification_type") != self.channel:
            return DeliveryResult("failed", error_code="channel_mismatch",
                                  error_message="Notification channel does not match Telegram.")
        recipient = notification.get("recipient")
        message = notification.get("notification_message")
        if not isinstance(recipient, str) or not recipient.strip() or not recipient.lstrip("-").isdigit():
            return DeliveryResult("failed", error_code="invalid_recipient",
                                  error_message="Telegram recipient must be a numeric chat ID.")
        if not isinstance(message, str) or not message.strip():
            return DeliveryResult("failed", error_code="empty_message",
                                  error_message="Notification message is empty.")
        try:
            response = self._transport.send_message(recipient, message)
        except TimeoutError:
            return DeliveryResult("retryable", error_code="transport_timeout",
                                  error_message="Telegram transport timed out.")
        except ConnectionError:
            return DeliveryResult("retryable", error_code="transport_unavailable",
                                  error_message="Telegram transport is unavailable.")
        except Exception:
            # Do not persist arbitrary exception text; it may contain credentials.
            return DeliveryResult("retryable", error_code="transport_error",
                                  error_message="Telegram transport failed unexpectedly.")

        if not isinstance(response, dict) or response.get("ok") is not True:
            error_code = response.get("error_code") if isinstance(response, dict) else None
            if error_code in {429, 500, 502, 503, 504}:
                return DeliveryResult("retryable", error_code=f"telegram_{error_code}",
                                      error_message="Telegram reported a temporary delivery error.")
            return DeliveryResult("failed", error_code="telegram_rejected",
                                  error_message="Telegram rejected the delivery request.")

        result = response.get("result") or {}
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id is None:
            return DeliveryResult("retryable", error_code="missing_message_id",
                                  error_message="Telegram response did not contain a message ID.")
        return DeliveryResult("sent", provider_message_id=str(message_id))
