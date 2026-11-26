"""Explicit orchestration boundary for notification generation and delivery.

Generation and delivery remain separate operations. Handling a shipment event
only validates it and writes eligible notifications to the outbox; it never
contacts a provider. Delivery requires a separately configured worker and an
explicit dispatch_pending() call.
"""
from notifications.generation import generate_notifications


class NotificationPipeline:
    def __init__(self, delivery_worker=None):
        if delivery_worker is not None and not callable(getattr(delivery_worker, "run_once", None)):
            raise TypeError("delivery_worker must provide run_once().")
        self._delivery_worker = delivery_worker

    def handle_shipment_change(self, event_payload):
        """Validate and enqueue notifications; never performs delivery."""
        return generate_notifications(event_payload)

    def dispatch_pending(self):
        """Explicitly dispatch one due batch using the configured worker."""
        if self._delivery_worker is None:
            raise RuntimeError("No delivery worker is configured.")
        return self._delivery_worker.run_once()
