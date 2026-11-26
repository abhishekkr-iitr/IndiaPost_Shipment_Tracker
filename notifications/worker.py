"""Durable notification worker with a final recipient-authorization gate."""
from database.notification_delivery_repository import (
    claim_due_notifications, complete_notification, retry_notification,
    fail_notification, fail_exhausted_notifications, is_notification_authorized,
)


class NotificationWorker:
    def __init__(self, worker_id, adapter_registry, *, batch_size=20,
                 lease_seconds=120, max_attempts=5, retry_delay_seconds=60):
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be a non-empty string.")
        if not callable(getattr(adapter_registry, "get", None)):
            raise TypeError("adapter_registry must provide get(channel).")
        for name, value in (("batch_size", batch_size), ("lease_seconds", lease_seconds),
                            ("max_attempts", max_attempts), ("retry_delay_seconds", retry_delay_seconds)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0 or (name != "retry_delay_seconds" and value == 0):
                raise ValueError(f"{name} has an invalid value.")
        self.worker_id = worker_id
        self.registry = adapter_registry
        self.batch_size = batch_size
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds

    def run_once(self):
        exhausted = fail_exhausted_notifications(self.max_attempts)
        claimed = claim_due_notifications(
            self.worker_id, self.batch_size, self.lease_seconds, self.max_attempts
        )
        counts = {"claimed": len(claimed), "sent": 0, "retry_scheduled": 0,
                  "failed": exhausted, "adapter_missing": 0,
                  "suppressed_unauthorized": 0, "authorization_check_error": 0}
        for notification in claimed:
            notification_id = notification["notification_id"]
            # Re-check consent, verification, subscription and exact destination
            # after claiming and immediately before any provider call.
            try:
                authorized = is_notification_authorized(notification_id)
            except Exception:
                retry_notification(notification_id, self.worker_id,
                                   "Recipient authorization could not be verified.",
                                   self.retry_delay_seconds)
                counts["retry_scheduled"] += 1
                counts["authorization_check_error"] += 1
                continue
            if not authorized:
                fail_notification(notification_id, self.worker_id,
                                  "Recipient authorization is no longer valid.")
                counts["failed"] += 1
                counts["suppressed_unauthorized"] += 1
                continue
            try:
                adapter = self.registry.get(notification["notification_type"])
            except LookupError:
                fail_notification(notification_id, self.worker_id,
                                  "No delivery adapter is registered for this channel.")
                counts["failed"] += 1
                counts["adapter_missing"] += 1
                continue
            try:
                result = adapter.deliver(notification)
            except Exception:
                retry_notification(notification_id, self.worker_id,
                                   "Delivery adapter raised an unexpected error.",
                                   self.retry_delay_seconds)
                counts["retry_scheduled"] += 1
                continue
            if result.status == "sent":
                complete_notification(notification_id, self.worker_id,
                                      result.provider_message_id)
                counts["sent"] += 1
            elif result.status == "retryable":
                if notification["attempt_count"] >= self.max_attempts:
                    fail_notification(notification_id, self.worker_id,
                                      result.error_message or "Maximum delivery attempts reached.")
                    counts["failed"] += 1
                else:
                    retry_notification(notification_id, self.worker_id,
                                       result.error_message or "Temporary delivery failure.",
                                       self.retry_delay_seconds)
                    counts["retry_scheduled"] += 1
            else:
                fail_notification(notification_id, self.worker_id,
                                  result.error_message or "Permanent delivery failure.")
                counts["failed"] += 1
        return counts
