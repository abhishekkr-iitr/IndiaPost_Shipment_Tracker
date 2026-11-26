"""Validation, capability enforcement and correlation for provider receipts.

This service is channel-independent. It performs no network I/O and never
changes notification delivery or retry state.
"""
from dataclasses import replace

from database.db_connection import get_db_connection
from database.notification_delivery_receipt_repository import record_provider_receipt
from notifications.receipts import ProviderReceipt, ReceiptCapabilityRegistry


def _correlate_attempt(notification_id, receipt):
    """Resolve exactly one matching delivery attempt; fail closed if ambiguous."""
    if receipt.attempt_id is not None:
        return receipt

    connection = get_db_connection()
    try:
        rows = connection.execute(
            """SELECT attempt_id
               FROM notification_delivery_attempts
               WHERE notification_id = ?
                 AND lower(trim(provider)) = ?
                 AND provider_message_id = ?
               ORDER BY attempt_id""",
            (notification_id, receipt.provider.strip().lower(),
             receipt.provider_message_id),
        ).fetchall()
    finally:
        connection.close()

    if not rows:
        raise ValueError(
            "Receipt could not be correlated to a delivery attempt."
        )
    if len(rows) != 1:
        raise ValueError(
            "Receipt matches multiple delivery attempts; explicit attempt_id is required."
        )
    return replace(receipt, attempt_id=rows[0][0])


def process_provider_receipt(notification_id, receipt, capability_registry):
    """Validate capability, correlate, and persist one provider receipt.

    The return value is the repository's persistence result. A repeated,
    identical provider event is an idempotent duplicate. Conflicting reuse of
    an event ID is rejected. Receipt processing never dispatches or retries.
    """
    if not isinstance(receipt, ProviderReceipt):
        raise TypeError("receipt must be a ProviderReceipt.")
    if not isinstance(capability_registry, ReceiptCapabilityRegistry):
        raise TypeError("capability_registry must be a ReceiptCapabilityRegistry.")

    capabilities = capability_registry.get(receipt.provider)
    if receipt.status == "read":
        if not capabilities.read_receipts:
            raise ValueError("Provider does not support read receipts.")
    elif not capabilities.delivery_receipts:
        raise ValueError("Provider does not support delivery receipts.")

    correlated_receipt = _correlate_attempt(notification_id, receipt)
    return record_provider_receipt(notification_id, correlated_receipt)
