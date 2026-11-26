"""Polling scheduler for the proactive shipment monitoring worker.

The scheduler only wakes the existing worker at a configurable cadence. It does
not select jobs, call tracking providers, update shipment state, or deliver
notifications; those responsibilities remain with the worker and its existing
repositories/services.
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from monitoring.monitoring_worker import MonitoringWorker


LOGGER = logging.getLogger(__name__)
DEFAULT_POLLING_INTERVAL_SECONDS = 60


class MonitoringScheduler:
    """Periodically invoke an existing monitoring worker."""

    def __init__(
        self,
        worker: "MonitoringWorker",
        *,
        polling_interval_seconds: int = DEFAULT_POLLING_INTERVAL_SECONDS,
        logger: logging.Logger | None = None,
    ):
        if not callable(getattr(worker, "run_and_process_once", None)):
            raise TypeError("worker must provide run_and_process_once().")
        if (
            not isinstance(polling_interval_seconds, (int, float))
            or isinstance(polling_interval_seconds, bool)
            or polling_interval_seconds <= 0
        ):
            raise ValueError("polling_interval_seconds must be a positive number.")
        self.worker = worker
        self.polling_interval_seconds = float(polling_interval_seconds)
        self.logger = logger or LOGGER

    def run_once(self) -> list[dict]:
        """Invoke one worker cycle and return its results unchanged."""
        return self.worker.run_and_process_once()

    def run_forever(
        self,
        stop_event: threading.Event | None = None,
        *,
        on_cycle: Callable[[list[dict]], None] | None = None,
    ) -> None:
        """Run worker cycles until stop_event is set.

        The first cycle runs immediately. The interval is a scheduler polling
        cadence, not a user's shipment monitoring interval. Worker exceptions
        are logged and do not terminate the scheduler; the next poll still runs.
        """
        if stop_event is None:
            stop_event = threading.Event()
        if not isinstance(stop_event, threading.Event):
            raise TypeError("stop_event must be a threading.Event.")
        if on_cycle is not None and not callable(on_cycle):
            raise TypeError("on_cycle must be callable when provided.")

        while not stop_event.is_set():
            try:
                results = self.run_once()
                if on_cycle is not None:
                    on_cycle(results)
            except Exception:
                self.logger.exception("Monitoring scheduler cycle failed; will retry on next poll.")
            if stop_event.wait(self.polling_interval_seconds):
                break
