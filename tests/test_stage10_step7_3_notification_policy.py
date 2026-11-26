import unittest
from types import SimpleNamespace

from notifications.policy import evaluate_checkpoint_notification
from notifications.generation import _notification_event, _render_message
from notifications.event_contract import SHIPMENT_CHECKPOINT_ADDED, EVENT_SCHEMA_VERSION_V2


class NotificationPolicyTests(unittest.TestCase):
    def checkpoint(self, status=None, message=None):
        return SimpleNamespace(status=status, message=message)

    def test_out_for_delivery_status_is_meaningful(self):
        result = evaluate_checkpoint_notification(self.checkpoint("OutForDelivery"))
        self.assertTrue(result.notify)
        self.assertEqual(result.category, "out_for_delivery")

    def test_delivered_status_is_meaningful(self):
        result = evaluate_checkpoint_notification(self.checkpoint("Delivered"))
        self.assertTrue(result.notify)
        self.assertEqual(result.category, "delivered")

    def test_explicit_arrival_at_delivery_office_is_meaningful(self):
        result = evaluate_checkpoint_notification(
            self.checkpoint("InTransit", "Item received at delivery office")
        )
        self.assertTrue(result.notify)
        self.assertEqual(result.category, "arrived_at_delivery_office")

    def test_routine_sorting_office_scan_is_suppressed(self):
        result = evaluate_checkpoint_notification(
            self.checkpoint("InTransit", "Item received at sorting office")
        )
        self.assertFalse(result.notify)

    def test_dispatched_intermediate_scan_is_suppressed(self):
        result = evaluate_checkpoint_notification(
            self.checkpoint("InTransit", "Shipment dispatched")
        )
        self.assertFalse(result.notify)

    def test_unknown_and_empty_values_fail_closed(self):
        for status, message in (("SomeUnknownTag", "Something happened"), (None, None)):
            with self.subTest(status=status, message=message):
                self.assertFalse(evaluate_checkpoint_notification(
                    self.checkpoint(status, message)
                ).notify)

    def test_checkpoint_event_normalizes_for_existing_notification_pipeline(self):
        payload = {
            "event_id": "checkpoint-event-1",
            "event_type": SHIPMENT_CHECKPOINT_ADDED,
            "schema_version": EVENT_SCHEMA_VERSION_V2,
            "source": "monitoring",
            "occurred_at": "2026-11-17T10:00:00+00:00",
            "recorded_at": "2026-11-17T10:01:00+00:00",
            "shipment": {"shipment_id": 1, "tracking_number": "AB123456789IN", "carrier": "india-post"},
            "change": {"checkpoint_id": 12, "checkpoint_event_at": "2026-11-17T10:00:00+00:00",
                       "location": "Bengaluru Delivery Office", "status": "OutForDelivery",
                       "message": "Item is out for delivery"},
        }
        event = _notification_event(payload)
        self.assertEqual(event.change.checkpoint_id, 12)
        self.assertEqual(event.change.new_status, "out_for_delivery")
        self.assertIn("Important shipment checkpoint", _render_message(event))

    def test_routine_checkpoint_event_is_not_notifiable(self):
        payload = {
            "event_id": "checkpoint-event-2", "event_type": SHIPMENT_CHECKPOINT_ADDED,
            "schema_version": EVENT_SCHEMA_VERSION_V2, "source": "webhook",
            "occurred_at": "2026-11-17T10:00:00+00:00", "recorded_at": "2026-11-17T10:01:00+00:00",
            "shipment": {"shipment_id": 1, "tracking_number": "AB123456789IN", "carrier": "india-post"},
            "change": {"checkpoint_id": 13, "location": "Delhi Sorting Office", "status": "InTransit",
                       "message": "Item received at sorting office"},
        }
        self.assertIsNone(_notification_event(payload))


if __name__ == "__main__":
    unittest.main()
