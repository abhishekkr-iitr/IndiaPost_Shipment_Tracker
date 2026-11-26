"""HTTP transport for Telegram Bot API message delivery.

The transport performs only the provider request. Notification validation,
policy, retries and persistence remain in the existing adapter/worker layers.
"""
from __future__ import annotations

from typing import Any

import httpx


class TelegramHTTPTransport:
    """Send Telegram messages through the Bot API using ``httpx``.

    ``client`` is injectable for deterministic tests. When omitted, the
    transport owns an httpx.Client and should be closed with ``close()``.
    The bot token is never included in returned errors or exception messages.
    """

    API_BASE_URL = "https://api.telegram.org"

    def __init__(self, bot_token: str, *, timeout: float = 10.0,
                 client: httpx.Client | None = None):
        if not isinstance(bot_token, str) or not bot_token.strip():
            raise ValueError("A Telegram bot token is required.")
        if any(char.isspace() for char in bot_token):
            raise ValueError("Telegram bot token contains invalid whitespace.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be a positive number of seconds.")
        self._bot_token = bot_token
        self._client = client or httpx.Client(timeout=float(timeout))
        self._owns_client = client is None

    def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
        """Call sendMessage and return a provider-shaped response dictionary."""
        url = f"{self.API_BASE_URL}/bot{self._bot_token}/sendMessage"
        try:
            response = self._client.post(
                url,
                json={"chat_id": chat_id, "text": text},
            )
        except httpx.TimeoutException as exc:
            raise TimeoutError("Telegram request timed out.") from None
        except httpx.RequestError as exc:
            # Do not propagate the original exception: its URL may contain the token.
            raise ConnectionError("Telegram service is temporarily unreachable.") from None

        try:
            payload = response.json()
        except (ValueError, TypeError):
            payload = None

        if isinstance(payload, dict):
            result = dict(payload)
        else:
            result = {"ok": False}

        # HTTP failures must not be mistaken for successful provider responses,
        # even if a proxy or intermediary returns an unexpected JSON body.
        if response.status_code != 200:
            result["ok"] = False
            result.setdefault("error_code", response.status_code)
        return result

    def close(self) -> None:
        """Close only the HTTP client created by this transport."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "TelegramHTTPTransport":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
