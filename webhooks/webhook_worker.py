"""Durable background worker for persisted AfterShip webhook events."""

import logging
import threading
import uuid

from database.webhook_event_repository import (
    claim_next_webhook_event,
    mark_webhook_event_failed,
)
from webhooks.webhook_config import load_webhook_config
from webhooks.webhook_event_processor import process_claimed_webhook_event


logger = logging.getLogger(__name__)


class WebhookWorker:
    """Claim and process durable webhook events until stopped."""

    def __init__(self, config=None, worker_id=None):
        self.config = config or load_webhook_config()

        if not isinstance(self.config.worker_poll_seconds, (int, float)):
            raise ValueError("Worker poll interval must be numeric.")

        if self.config.worker_poll_seconds <= 0:
            raise ValueError("Worker poll interval must be positive.")

        if (
            isinstance(self.config.worker_lease_seconds, bool)
            or not isinstance(self.config.worker_lease_seconds, int)
            or self.config.worker_lease_seconds <= 0
        ):
            raise ValueError("Worker lease duration must be a positive integer.")

        if (
            isinstance(self.config.max_processing_attempts, bool)
            or not isinstance(self.config.max_processing_attempts, int)
            or self.config.max_processing_attempts <= 0
        ):
            raise ValueError("Maximum processing attempts must be a positive integer.")

        if not isinstance(self.config.retry_delays_seconds, (tuple, list)):
            raise ValueError("Retry delays must be a tuple or list.")

        if not self.config.retry_delays_seconds:
            raise ValueError("At least one retry delay is required.")

        if worker_id is None:
            worker_id = f"webhook-worker-{uuid.uuid4().hex}"

        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("Worker ID must be a non-empty string.")

        self.worker_id = worker_id.strip()

    def process_next_event(self):
        """
        Claim and process one event.

        Returns None when no event is currently eligible.
        Returns a result dictionary for a processed or failed event.
        """

        event = claim_next_webhook_event(
            worker_id=self.worker_id,
            lease_seconds=self.config.worker_lease_seconds,
            max_attempts=self.config.max_processing_attempts,
        )

        if event is None:
            return None

        event_record_id = event.get("event_record_id")

        if not event_record_id:
            raise RuntimeError(
                "Claimed webhook event has no event record ID."
            )

        try:
            result = process_claimed_webhook_event(
                event_record_id=event_record_id,
                worker_id=self.worker_id,
            )

            return {
                "event_record_id": event_record_id,
                "status": "processed",
                "result": result,
            }

        except Exception as exc:
            # Store a bounded diagnostic. Never log or include the raw
            # webhook payload or configured webhook secret.
            diagnostic = (
                f"{type(exc).__name__}: {exc}"
            )[:2000]

            failure_result = mark_webhook_event_failed(
                event_record_id=event_record_id,
                worker_id=self.worker_id,
                error=diagnostic,
                retry_delays=self.config.retry_delays_seconds,
                max_attempts=self.config.max_processing_attempts,
            )

            logger.warning(
                "Webhook event processing failed: event_record_id=%s status=%s",
                event_record_id,
                failure_result.get("status", "unknown"),
            )

            return {
                "event_record_id": event_record_id,
                "status": failure_result["status"],
                "failure": failure_result,
            }

    def run(self, stop_event=None):
        """
        Run the polling loop until stop_event is set.

        A threading.Event can be supplied for graceful shutdown.
        If omitted, a private event is created and the worker runs
        until interrupted by the hosting process.
        """

        if not self.config.enabled:
            raise RuntimeError(
                "Webhook worker is disabled. Set WEBHOOK_ENABLED=true "
                "and configure AFTERSHIP_WEBHOOK_SECRET."
            )

        if stop_event is None:
            stop_event = threading.Event()

        if not isinstance(stop_event, threading.Event):
            raise ValueError("stop_event must be a threading.Event.")

        logger.info(
            "Webhook worker started: worker_id=%s",
            self.worker_id,
        )

        try:
            while not stop_event.is_set():
                try:
                    result = self.process_next_event()

                    if result is None:
                        stop_event.wait(
                            self.config.worker_poll_seconds
                        )

                except Exception:
                    # A repository/claiming failure is not an event-level
                    # processing failure. Keep the worker alive and retry
                    # after the configured polling interval.
                    logger.exception(
                        "Webhook worker iteration failed: worker_id=%s",
                        self.worker_id,
                    )
                    stop_event.wait(
                        self.config.worker_poll_seconds
                    )

        except KeyboardInterrupt:
            logger.info(
                "Webhook worker interrupted: worker_id=%s",
                self.worker_id,
            )

        finally:
            logger.info(
                "Webhook worker stopped: worker_id=%s",
                self.worker_id,
            )


def run_worker(config=None, stop_event=None, worker_id=None):
    """Convenience entry point for running a webhook worker."""

    worker = WebhookWorker(
        config=config,
        worker_id=worker_id,
    )
    worker.run(stop_event=stop_event)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_worker()
