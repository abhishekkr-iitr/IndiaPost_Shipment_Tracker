"""Channel-independent notification delivery contracts and adapter registry.

This module deliberately contains no network client and performs no delivery itself.
"""
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    provider_message_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self):
        if self.status not in {"sent", "retryable", "failed"}:
            raise ValueError("Unsupported delivery result status.")
        if self.status == "sent" and self.error_code:
            raise ValueError("A successful result cannot contain an error code.")


@runtime_checkable
class DeliveryAdapter(Protocol):
    channel: str

    def deliver(self, notification: dict) -> DeliveryResult:
        """Attempt delivery of one queued notification."""


class AdapterRegistry:
    """Explicit channel-to-adapter registry; no implicit fallback is allowed."""

    def __init__(self):
        self._adapters = {}

    def register(self, adapter: DeliveryAdapter):
        channel = getattr(adapter, "channel", None)
        if not isinstance(channel, str) or not channel.strip():
            raise ValueError("Adapter must declare a non-empty channel.")
        if not callable(getattr(adapter, "deliver", None)):
            raise ValueError("Adapter must implement deliver().")
        if channel in self._adapters:
            raise ValueError(f"An adapter is already registered for {channel}.")
        self._adapters[channel] = adapter

    def get(self, channel: str) -> DeliveryAdapter:
        try:
            return self._adapters[channel]
        except KeyError as exc:
            raise LookupError(f"No delivery adapter registered for {channel}.") from exc

    def channels(self):
        return tuple(sorted(self._adapters))
