"""Runnable entry point for the lightweight monitoring scheduler."""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
from pathlib import Path

from dotenv import load_dotenv

from monitoring.monitoring_scheduler import DEFAULT_POLLING_INTERVAL_SECONDS, MonitoringScheduler
from monitoring.monitoring_worker import MonitoringWorker

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


def main() -> None:
    # Load this project's .env without overriding OS/hosting environment values.
    load_dotenv(dotenv_path=BASE_DIR / ".env", override=False)
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger("monitoring.scheduler.runner")
    default_worker_id = f"monitoring-{socket.gethostname()}-{os.getpid()}"
    worker_id = os.getenv("MONITORING_WORKER_ID", default_worker_id).strip()
    if not worker_id:
        raise ValueError("MONITORING_WORKER_ID must not be blank.")
    worker = MonitoringWorker(
        worker_id,
        batch_size=_positive_int_env("MONITORING_BATCH_SIZE", 10),
        lease_seconds=_positive_int_env("MONITORING_LEASE_SECONDS", 300),
    )
    scheduler = MonitoringScheduler(
        worker,
        polling_interval_seconds=_positive_int_env(
            "MONITORING_SCHEDULER_POLL_SECONDS", DEFAULT_POLLING_INTERVAL_SECONDS
        ),
    )
    stop_event = threading.Event()

    def request_stop(signum, _frame):
        logger.info("Received signal %s; stopping monitoring scheduler.", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)
    logger.info(
        "Starting monitoring scheduler (worker_id=%s, poll_seconds=%s).",
        worker_id, scheduler.polling_interval_seconds,
    )
    scheduler.run_forever(stop_event)
    logger.info("Monitoring scheduler stopped.")


if __name__ == "__main__":
    main()
