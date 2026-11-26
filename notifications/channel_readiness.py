"""Read-only channel capability/readiness reporting.

This module does not send messages, inspect or expose credentials, mutate
preferences, or alter the adapter registry. Configuration readiness is supplied
by the application bootstrap after its own provider-specific validation.
"""
from __future__ import annotations

SUPPORTED_DELIVERY_CHANNELS = frozenset({"telegram", "email", "sms", "whatsapp"})


def get_channel_readiness(adapter_registry, *, configured_channels=()):
    """Report adapter registration and configuration readiness per channel.

    ``configured_channels`` must contain channel names whose provider
    configuration has already been validated by the caller. A channel is
    operationally ready only when both an adapter is registered and its
    configuration is validated.

    This is a local capability assessment, not a live provider health check.
    """
    if not callable(getattr(adapter_registry, "channels", None)):
        raise TypeError("adapter_registry must provide channels().")

    if isinstance(configured_channels, (str, bytes)):
        raise TypeError("configured_channels must be an iterable of channel names.")

    try:
        configured = set(configured_channels)
    except TypeError as exc:
        raise TypeError("configured_channels must be an iterable of channel names.") from exc

    if any(not isinstance(channel, str) or channel not in SUPPORTED_DELIVERY_CHANNELS
           for channel in configured):
        raise ValueError("configured_channels contains an unsupported channel.")

    registered_values = adapter_registry.channels()
    registered = set(registered_values)
    if any(not isinstance(channel, str) or channel not in SUPPORTED_DELIVERY_CHANNELS
           for channel in registered):
        raise ValueError("Adapter registry contains an unsupported channel.")

    channels = {}
    for channel in sorted(SUPPORTED_DELIVERY_CHANNELS):
        adapter_registered = channel in registered
        configuration_validated = channel in configured
        if adapter_registered and configuration_validated:
            status = "ready"
        elif not adapter_registered:
            status = "adapter_not_registered"
        else:
            status = "configuration_not_validated"

        channels[channel] = {
            "adapter_registered": adapter_registered,
            "configuration_validated": configuration_validated,
            "ready": status == "ready",
            "status": status,
        }

    return {
        "channels": channels,
        "ready_channels": sorted(
            channel for channel, details in channels.items() if details["ready"]
        ),
        "unavailable_channels": sorted(
            channel for channel, details in channels.items() if not details["ready"]
        ),
        "live_provider_health_checked": False,
    }
