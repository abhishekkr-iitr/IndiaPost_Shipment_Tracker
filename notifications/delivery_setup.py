"""Build the configured notification delivery runtime.

This module wires the existing Telegram adapter to the HTTP transport while
leaving notification generation, authorization, persistence and worker policy
unchanged. It never sends a message during construction.
"""
from __future__ import annotations

import os
from typing import Mapping

from notifications.delivery import AdapterRegistry
from notifications.telegram_adapter import TelegramAdapter
from notifications.telegram_transport import TelegramHTTPTransport


class DeliveryRuntime:
    """Owns the adapter registry and transports created for that registry."""

    def __init__(self, registry: AdapterRegistry, transports: tuple):
        self.registry = registry
        self._transports = transports
        self._closed = False

    def close(self) -> None:
        """Close owned transports; safe to call more than once."""
        if self._closed:
            return
        for transport in self._transports:
            close = getattr(transport, "close", None)
            if callable(close):
                close()
        self._closed = True

    def __enter__(self) -> "DeliveryRuntime":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def build_delivery_runtime(
    *,
    bot_token: str | None = None,
    http_client=None,
    timeout: float = 10.0,
    environ: Mapping[str, str] | None = None,
) -> DeliveryRuntime:
    """Create a registry with the Telegram adapter and HTTP transport.

    The token is read from TELEGRAM_BOT_TOKEN unless explicitly injected.
    Supplying ``http_client`` enables mocked tests and does not transfer
    ownership of that client to the runtime.
    """
    source = os.environ if environ is None else environ
    token = bot_token if bot_token is not None else source.get("TELEGRAM_BOT_TOKEN")
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("Telegram delivery is not configured: TELEGRAM_BOT_TOKEN is missing.")

    transport = TelegramHTTPTransport(token, timeout=timeout, client=http_client)
    adapter = TelegramAdapter(transport)
    registry = AdapterRegistry()
    registry.register(adapter)
    return DeliveryRuntime(registry, (transport,))
