import unittest

from notifications.event_contract import (
    EVENT_SCHEMA_VERSION_V2,
    SHIPMENT_CHECKPOINT_ADDED,
    SHIPMENT_STATUS_CHANGED,
    ShipmentChangeEvent,
    ShipmentChangeEventV2,
    ShipmentCheckpointDetails,
    ShipmentReference,
    ShipmentStatusTransition,
    parse_shipment_change_event,
)


class Stage10ChangeEventContractV2Tests(unittest.TestCase):
    def shipment(self):
        return ShipmentReference(8, "AB123456789IN", "india-post")

    def test_v1_event_remains_parseable_through_dispatcher(self):
        from notifications.event_contract import ShipmentChange
        event = ShipmentChangeEvent(
            "legacy-event-1", SHIPMENT_STATUS_CHANGED, 1, "aftership_webhook",
            "2026-11-16T10:00:00+00:00", "2026-11-16T10:00:01+00:00",
            self.shipment(), ShipmentChange("In Transit", "Out for Delivery"),
        )
        restored = parse_shipment_change_event(event.to_dict())
        self.assertIsInstance(restored, ShipmentChangeEvent)
        self.assertEqual(restored, event)

    def test_v2_status_event_round_trip(self):
        event = ShipmentChangeEventV2(
            "status-event-1", SHIPMENT_STATUS_CHANGED, EVENT_SCHEMA_VERSION_V2,
            "monitoring", "2026-11-16T10:00:00+00:00",
            "2026-11-16T10:00:02+00:00", self.shipment(),
            ShipmentStatusTransition("In Transit", "Out for Delivery"),
        )
        restored = parse_shipment_change_event(event.to_dict())
        self.assertEqual(restored, event)

    def test_v2_checkpoint_only_event_round_trip(self):
        event = ShipmentChangeEventV2(
            "checkpoint-event-1", SHIPMENT_CHECKPOINT_ADDED,
            EVENT_SCHEMA_VERSION_V2, "webhook",
            "2026-11-16T09:45:00+00:00", "2026-11-16T10:00:02+00:00",
            self.shipment(),
            ShipmentCheckpointDetails(
                125, "2026-11-16T09:45:00+00:00", "Bengaluru",
                "In Transit", "Item processed at facility",
            ),
        )
        restored = parse_shipment_change_event(event.to_dict())
        self.assertEqual(restored, event)
        self.assertEqual(restored.change.checkpoint_id, 125)

    def test_v2_rejects_status_event_with_checkpoint_payload(self):
        payload = ShipmentChangeEventV2(
            "status-event-2", SHIPMENT_STATUS_CHANGED, 2, "webhook",
            "2026-11-16T09:45:00+00:00", "2026-11-16T10:00:02+00:00",
            self.shipment(), ShipmentStatusTransition("In Transit", "Delivered"),
        ).to_dict()
        payload["change"] = {"checkpoint_id": 125}
        with self.assertRaises(ValueError):
            parse_shipment_change_event(payload)

    def test_v2_rejects_invalid_source(self):
        with self.assertRaises(ValueError):
            ShipmentChangeEventV2(
                "event-3", SHIPMENT_STATUS_CHANGED, 2, "telegram",
                "2026-11-16T09:45:00+00:00", "2026-11-16T10:00:02+00:00",
                self.shipment(), ShipmentStatusTransition("In Transit", "Delivered"),
            )

    def test_v2_rejects_checkpoint_without_canonical_id(self):
        payload = {
            "event_id": "checkpoint-event-2",
            "event_type": SHIPMENT_CHECKPOINT_ADDED,
            "schema_version": 2,
            "source": "monitoring",
            "occurred_at": "2026-11-16T09:45:00+00:00",
            "recorded_at": "2026-11-16T10:00:02+00:00",
            "shipment": {"shipment_id": 8, "tracking_number": "AB123456789IN", "carrier": "india-post"},
            "change": {"location": "Bengaluru"},
        }
        with self.assertRaises(ValueError):
            parse_shipment_change_event(payload)

    def test_v2_rejects_timezone_naive_event_timestamp(self):
        with self.assertRaises(ValueError):
            ShipmentChangeEventV2(
                "event-4", SHIPMENT_STATUS_CHANGED, 2, "monitoring",
                "2026-11-16T09:45:00", "2026-11-16T10:00:02+00:00",
                self.shipment(), ShipmentStatusTransition("In Transit", "Delivered"),
            )


if __name__ == "__main__":
    unittest.main()
