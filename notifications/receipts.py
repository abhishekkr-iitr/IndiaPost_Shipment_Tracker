"""Channel-independent provider receipt contracts and capability registry.

This module defines receipt data and provider capabilities only. It performs no
network I/O, webhook authentication, persistence, or delivery.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


SUPPORTED_RECEIPT_STATUSES = frozenset({
    "submitted",
    "delivered",
    "failed",
    "read",
})


@dataclass(frozen=True)
class ReceiptCapabilities:
    """Receipt features explicitly supported by a provider integration."""

    delivery_receipts: bool = False
    read_receipts: bool = False

    def __post_init__(self):
        if not isinstance(self.delivery_receipts, bool):
            raise TypeError("delivery_receipts must be a boolean.")
        if not isinstance(self.read_receipts, bool):
            raise TypeError("read_receipts must be a boolean.")
        if self.read_receipts and not self.delivery_receipts:
            raise ValueError("Read receipts require delivery receipt support.")


@dataclass(frozen=True)
class ProviderReceipt:
    """Normalized provider status event, independent of provider payload shape."""

    provider: str
    provider_event_id: str
    provider_message_id: str
    status: str
    provider_event_at: datetime
    attempt_id: Optional[int] = None

    def __post_init__(self):
        for field_name in (
            "provider", "provider_event_id", "provider_message_id"
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string.")
        if self.status not in SUPPORTED_RECEIPT_STATUSES:
            raise ValueError("Unsupported provider receipt status.")
        if not isinstance(self.provider_event_at, datetime):
            raise TypeError("provider_event_at must be a datetime.")
        if self.provider_event_at.tzinfo is None:
            raise ValueError("provider_event_at must be timezone-aware.")
        if self.attempt_id is not None:
            if (not isinstance(self.attempt_id, int)
                    or isinstance(self.attempt_id, bool)
                    or self.attempt_id <= 0):
                raise ValueError("attempt_id must be a positive integer.")


class ReceiptCapabilityRegistry:
    """Explicit provider-to-capability registry; no inferred capabilities."""

    def __init__(self):
        self._capabilities = {}

    def register(self, provider, capabilities):
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("provider must be a non-empty string.")
        if not isinstance(capabilities, ReceiptCapabilities):
            raise TypeError("capabilities must be ReceiptCapabilities.")
        key = provider.strip().lower()
        if key in self._capabilities:
            raise ValueError(f"Receipt capabilities already registered for {key}.")
        self._capabilities[key] = capabilities

    def get(self, provider):
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("provider must be a non-empty string.")
        try:
            return self._capabilities[provider.strip().lower()]
        except KeyError as exc:
            raise LookupError(
                f"No receipt capabilities registered for {provider.strip().lower()}."
            ) from exc

    def providers(self):
        return tuple(sorted(self._capabilities))
