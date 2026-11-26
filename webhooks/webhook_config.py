"""Configuration for the AfterShip webhook receiver and worker."""

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class WebhookConfig:
    enabled: bool
    secret: str
    max_body_bytes: int
    worker_poll_seconds: float
    max_processing_attempts: int
    worker_lease_seconds: int
    retry_delays_seconds: tuple[int, ...]


def _read_int(name, default, minimum, maximum):
    raw = os.getenv(name)

    if raw is None:
        return default

    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc

    if not minimum <= value <= maximum:
        raise ValueError(
            f"{name} must be between {minimum} and {maximum}."
        )

    return value


def _read_float(name, default, minimum, maximum):
    raw = os.getenv(name)

    if raw is None:
        return default

    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric.") from exc

    if not minimum <= value <= maximum:
        raise ValueError(
            f"{name} must be between {minimum} and {maximum}."
        )

    return value


def _read_bool(name, default):
    raw = os.getenv(name)

    if raw is None:
        return default

    value = raw.strip().lower()

    if value in {"true", "1", "yes"}:
        return True

    if value in {"false", "0", "no"}:
        return False

    raise ValueError(f"{name} must be true or false.")


def load_webhook_config():
    """Load and validate webhook configuration from environment variables."""

    enabled = _read_bool("WEBHOOK_ENABLED", False)

    secret = os.getenv("AFTERSHIP_WEBHOOK_SECRET", "").strip()

    if enabled and not secret:
        raise ValueError(
            "AFTERSHIP_WEBHOOK_SECRET is required when "
            "WEBHOOK_ENABLED is true."
        )

    max_attempts = _read_int(
        "WEBHOOK_MAX_PROCESSING_ATTEMPTS", 5, 1, 20
    )

    retry_delays = (60, 300, 900, 3600, 21600)

    if max_attempts > len(retry_delays):
        raise ValueError(
            "WEBHOOK_MAX_PROCESSING_ATTEMPTS cannot exceed "
            f"{len(retry_delays)}."
        )

    return WebhookConfig(
        enabled=enabled,
        secret=secret,
        max_body_bytes=_read_int(
            "WEBHOOK_MAX_BODY_BYTES", 1_048_576, 1024, 10_485_760
        ),
        worker_poll_seconds=_read_float(
            "WEBHOOK_WORKER_POLL_SECONDS", 2.0, 0.1, 60.0
        ),
        max_processing_attempts=max_attempts,
        worker_lease_seconds=_read_int(
            "WEBHOOK_WORKER_LEASE_SECONDS", 300, 30, 3600
        ),
        retry_delays_seconds=retry_delays[:max_attempts],
    )