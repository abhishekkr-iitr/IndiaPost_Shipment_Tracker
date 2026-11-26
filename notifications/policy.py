"""Conservative, channel-independent notification intelligence policy.

V1 policy: all existing status-change notifications remain eligible. Checkpoint
notifications are allowed only for explicitly recognised high-value signals.
Unknown and routine checkpoint observations are suppressed by default.
"""
from dataclasses import dataclass
import re

POLICY_VERSION = "1.0"

@dataclass(frozen=True)
class CheckpointNotificationDecision:
    notify: bool
    category: str
    reason: str


def _normalise(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def evaluate_checkpoint_notification(checkpoint):
    """Return a fail-closed decision for a normalized V2 checkpoint payload.

    Explicit status tags are trusted only when exactly recognised. Free-text
    matching is limited to unambiguous delivery-office arrival phrases.
    Generic "office", "sorting office", "received", and "dispatched" phrases
    are intentionally insufficient.
    """
    status = _normalise(getattr(checkpoint, "status", None))
    message = str(getattr(checkpoint, "message", None) or "").casefold()
    if status == "outfordelivery":
        return CheckpointNotificationDecision(True, "out_for_delivery", "recognised_status")
    if status == "delivered":
        return CheckpointNotificationDecision(True, "delivered", "recognised_status")

    compact = re.sub(r"\s+", " ", message).strip()
    delivery_office_patterns = (
        r"\b(arrived|arrival|reached|received)\b.{0,45}\b(delivery office|delivery post office)\b",
        r"\b(delivery office|delivery post office)\b.{0,45}\b(arrived|arrival|received)\b",
    )
    if any(re.search(pattern, compact) for pattern in delivery_office_patterns):
        return CheckpointNotificationDecision(True, "arrived_at_delivery_office", "explicit_message")
    return CheckpointNotificationDecision(False, "suppressed", "routine_or_unclassified")
