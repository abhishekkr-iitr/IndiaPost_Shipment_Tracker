"""Polling scheduler for the durable notification delivery pipeline."""
from __future__ import annotations

import logging
import threading
from typing import Callable

LOGGER = logging.getLogger(__name__)
DEFAULT_POLLING_INTERVAL_SECONDS = 5


class NotificationScheduler:
    """Periodically dispatch one due notification batch."""

    def __init__(
        self,
        pipeline,
        *,
        polling_interval_seconds: int | float = DEFAULT_POLLING_INTERVAL_SECONDS,
        logger: logging.Logger | None = None,
    ):
        if not callable(getattr(pipeline, "dispatch_pending", None)):
            raise TypeError("pipeline must provide dispatch_pending().")
        if (
            not isinstance(polling_interval_seconds, (int, float))
            or isinstance(polling_interval_seconds, bool)
            or polling_interval_seconds <= 0
        ):
            raise ValueError("polling_interval_seconds must be a positive number.")
        self.pipeline = pipeline
        self.polling_interval_seconds = float(polling_interval_seconds)
        self.logger = logger or LOGGER

    def run_once(self):
        """Dispatch one batch and return the pipeline result unchanged."""
        return self.pipeline.dispatch_pending()

    def run_forever(
        self,
        stop_event: threading.Event | None = None,
        *,
        on_cycle: Callable[[object], None] | None = None,
    ) -> None:
        """Run immediately, then poll until stopped; recover from cycle errors."""
        if stop_event is None:
            stop_event = threading.Event()
        if not isinstance(stop_event, threading.Event):
            raise TypeError("stop_event must be a threading.Event.")
        if on_cycle is not None and not callable(on_cycle):
            raise TypeError("on_cycle must be callable when provided.")

        while not stop_event.is_set():
            try:
                result = self.run_once()
                if on_cycle is not None:
                    on_cycle(result)
            except Exception:
                self.logger.exception(
                    "Notification scheduler cycle failed; will retry on next poll."
                )
            if stop_event.wait(self.polling_interval_seconds):
                break
