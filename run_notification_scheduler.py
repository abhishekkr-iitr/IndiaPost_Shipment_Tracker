"""Runnable entry point for continuous notification delivery."""
from __future__ import annotations

import logging
import math
import os
import signal
import socket
import threading
from pathlib import Path

from dotenv import load_dotenv

from notifications.delivery_setup import build_delivery_runtime
from notifications.notification_scheduler import DEFAULT_POLLING_INTERVAL_SECONDS, NotificationScheduler
from notifications.pipeline import NotificationPipeline
from notifications.worker import NotificationWorker

BASE_DIR = Path(__file__).resolve().parent


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer.") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _nonnegative_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a non-negative integer.") from exc
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer.")
    return value


def _positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number.") from exc
    if value <= 0 or not math.isfinite(value):
        raise ValueError(f"{name} must be a positive finite number.")
    return value


def main() -> None:
    # Load this project's .env without overriding OS/hosting environment values.
    load_dotenv(dotenv_path=BASE_DIR / ".env", override=False)
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger("notifications.scheduler.runner")
    default_worker_id = f"notification-{socket.gethostname()}-{os.getpid()}"
    worker_id = os.getenv("NOTIFICATION_WORKER_ID", default_worker_id).strip()
    if not worker_id:
        raise ValueError("NOTIFICATION_WORKER_ID must not be blank.")
    batch_size = _positive_int_env("NOTIFICATION_BATCH_SIZE", 20)
    lease_seconds = _positive_int_env("NOTIFICATION_LEASE_SECONDS", 120)
    max_attempts = _positive_int_env("NOTIFICATION_MAX_ATTEMPTS", 5)
    retry_delay_seconds = _nonnegative_int_env("NOTIFICATION_RETRY_DELAY_SECONDS", 60)
    poll_seconds = _positive_float_env(
        "NOTIFICATION_SCHEDULER_POLL_SECONDS", DEFAULT_POLLING_INTERVAL_SECONDS
    )
    http_timeout = _positive_float_env("TELEGRAM_HTTP_TIMEOUT_SECONDS", 10.0)
    try:
        runtime = build_delivery_runtime(timeout=http_timeout)
    except RuntimeError as exc:
        logger.warning(
            "%s Notification scheduler is standing by (idle) until credentials are configured in .env.",
            exc,
        )
        stop_event = threading.Event()

        def request_standby_stop(signum, _frame):
            logger.info("Received signal %s; stopping notification scheduler standby.", signum)
            stop_event.set()

        signal.signal(signal.SIGINT, request_standby_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, request_standby_stop)
        stop_event.wait()
        logger.info("Notification scheduler standby stopped.")
        return
    try:
        worker = NotificationWorker(
            worker_id, runtime.registry, batch_size=batch_size,
            lease_seconds=lease_seconds, max_attempts=max_attempts,
            retry_delay_seconds=retry_delay_seconds,
        )
        pipeline = NotificationPipeline(delivery_worker=worker)
        scheduler = NotificationScheduler(pipeline, polling_interval_seconds=poll_seconds)
        stop_event = threading.Event()

        def request_stop(signum, _frame):
            logger.info("Received signal %s; stopping notification scheduler.", signum)
            stop_event.set()

        signal.signal(signal.SIGINT, request_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, request_stop)
        logger.info(
            "Starting notification scheduler (worker_id=%s, poll_seconds=%s, batch_size=%s).",
            worker_id, scheduler.polling_interval_seconds, batch_size,
        )

        def log_cycle(result):
            if isinstance(result, dict):
                # Aggregate counters only; never log recipients or message bodies.
                logger.info(
                    "Notification cycle completed (claimed=%s, sent=%s, retry_scheduled=%s, failed=%s).",
                    result.get("claimed", 0), result.get("sent", 0),
                    result.get("retry_scheduled", 0), result.get("failed", 0),
                )

        scheduler.run_forever(stop_event, on_cycle=log_cycle)
        logger.info("Notification scheduler stopped.")
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
