import unittest

from notifications.event_contract import (
    EVENT_SCHEMA_VERSION,
    SHIPMENT_STATUS_CHANGED,
    ShipmentChange,
    ShipmentChangeEvent,
    ShipmentChangeEventValidationError,
    ShipmentReference,
)


class ShipmentChangeEventContractTests(unittest.TestCase):
    def make_event(self):
        return ShipmentChangeEvent(
            event_id="shipment-checkpoint-501-status-change",
            event_type=SHIPMENT_STATUS_CHANGED,
            schema_version=EVENT_SCHEMA_VERSION,
            source="aftership_webhook",
            occurred_at="2026-11-16T10:30:00+05:30",
            recorded_at="2026-11-16T10:30:04+05:30",
            shipment=ShipmentReference(
                shipment_id=501,
                tracking_number="AB123456789IN",
                carrier="india-post",
            ),
            change=ShipmentChange(
                previous_status="In Transit",
                new_status="Out for Delivery",
                checkpoint_id=9001,
                checkpoint_event_at="2026-11-16T10:30:00+05:30",
                checkpoint_location="Bengaluru",
                checkpoint_message="Item out for delivery",
            ),
        )

    def test_valid_event_round_trips_to_dictionary(self):
        event = self.make_event()
        restored = ShipmentChangeEvent.from_dict(event.to_dict())
        self.assertEqual(restored, event)

    def test_first_observed_status_allows_missing_previous_status(self):
        event = self.make_event()
        event = ShipmentChangeEvent(
            **{**event.__dict__, "change": ShipmentChange(None, "In Transit")}
        )
        self.assertIsNone(event.change.previous_status)

    def test_contract_does_not_contain_recipient_or_channel(self):
        payload = self.make_event().to_dict()
        self.assertNotIn("recipient", payload)
        self.assertNotIn("channel", payload)
        self.assertNotIn("user_id", payload)

    def test_rejects_blank_event_id(self):
        with self.assertRaises(ShipmentChangeEventValidationError):
            ShipmentChangeEvent(
                " ", SHIPMENT_STATUS_CHANGED, 1, "poll",
                "2026-11-16T10:30:00+00:00", "2026-11-16T10:30:01+00:00",
                ShipmentReference(1, "AB123456789IN", "india-post"),
                ShipmentChange(None, "In Transit"),
            )

    def test_rejects_unsupported_event_type(self):
        event = self.make_event()
        with self.assertRaises(ShipmentChangeEventValidationError):
            ShipmentChangeEvent(
                event.event_id, "shipment.deleted", 1, event.source,
                event.occurred_at, event.recorded_at, event.shipment, event.change,
            )

    def test_rejects_wrong_schema_version(self):
        event = self.make_event()
        with self.assertRaises(ShipmentChangeEventValidationError):
            ShipmentChangeEvent(
                event.event_id, event.event_type, 2, event.source,
                event.occurred_at, event.recorded_at, event.shipment, event.change,
            )

    def test_rejects_timestamp_without_timezone(self):
        event = self.make_event()
        with self.assertRaises(ShipmentChangeEventValidationError):
            ShipmentChangeEvent(
                event.event_id, event.event_type, 1, event.source,
                "2026-11-16T10:30:00", event.recorded_at,
                event.shipment, event.change,
            )

    def test_rejects_invalid_shipment_id(self):
        with self.assertRaises(ShipmentChangeEventValidationError):
            ShipmentReference(True, "AB123456789IN", "india-post")

    def test_rejects_blank_new_status(self):
        with self.assertRaises(ShipmentChangeEventValidationError):
            ShipmentChange(None, " ")

    def test_rejects_unknown_top_level_field(self):
        payload = self.make_event().to_dict()
        payload["recipient"] = "user@example.com"
        with self.assertRaises(ShipmentChangeEventValidationError):
            ShipmentChangeEvent.from_dict(payload)

    def test_rejects_missing_required_field(self):
        payload = self.make_event().to_dict()
        del payload["event_id"]
        with self.assertRaises(ShipmentChangeEventValidationError):
            ShipmentChangeEvent.from_dict(payload)

    def test_rejects_unknown_change_field(self):
        payload = self.make_event().to_dict()
        payload["change"]["telegram_chat_id"] = "123"
        with self.assertRaises(ShipmentChangeEventValidationError):
            ShipmentChangeEvent.from_dict(payload)

    def test_event_id_is_preserved_exactly_for_idempotency(self):
        event = self.make_event()
        self.assertEqual(ShipmentChangeEvent.from_dict(event.to_dict()).event_id, event.event_id)


if __name__ == "__main__":
    unittest.main()
