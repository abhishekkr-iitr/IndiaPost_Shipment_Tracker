import unittest
from datetime import datetime, timezone

from notifications.receipts import (
    ProviderReceipt,
    ReceiptCapabilities,
    ReceiptCapabilityRegistry,
)


class ReceiptCapabilitiesTests(unittest.TestCase):
    def test_default_capabilities_are_unsupported(self):
        self.assertEqual(
            ReceiptCapabilities(),
            ReceiptCapabilities(delivery_receipts=False, read_receipts=False),
        )

    def test_read_receipts_require_delivery_receipts(self):
        with self.assertRaises(ValueError):
            ReceiptCapabilities(delivery_receipts=False, read_receipts=True)

    def test_capability_flags_must_be_boolean(self):
        with self.assertRaises(TypeError):
            ReceiptCapabilities(delivery_receipts=1)

    def test_registry_registers_and_normalizes_provider_name(self):
        registry = ReceiptCapabilityRegistry()
        caps = ReceiptCapabilities(delivery_receipts=True, read_receipts=True)
        registry.register(" WhatsApp ", caps)
        self.assertIs(registry.get("whatsapp"), caps)
        self.assertEqual(registry.providers(), ("whatsapp",))

    def test_registry_rejects_duplicate_provider(self):
        registry = ReceiptCapabilityRegistry()
        registry.register("provider-x", ReceiptCapabilities())
        with self.assertRaises(ValueError):
            registry.register("PROVIDER-X", ReceiptCapabilities())

    def test_registry_does_not_infer_unknown_provider_capabilities(self):
        with self.assertRaises(LookupError):
            ReceiptCapabilityRegistry().get("unknown")

    def test_registry_rejects_invalid_capability_object(self):
        with self.assertRaises(TypeError):
            ReceiptCapabilityRegistry().register("provider-x", object())


class ProviderReceiptTests(unittest.TestCase):
    def make_receipt(self, **overrides):
        values = {
            "provider": "whatsapp",
            "provider_event_id": "evt-001",
            "provider_message_id": "msg-001",
            "status": "delivered",
            "provider_event_at": datetime.now(timezone.utc),
            "attempt_id": 1,
        }
        values.update(overrides)
        return ProviderReceipt(**values)

    def test_valid_receipt_is_accepted(self):
        receipt = self.make_receipt()
        self.assertEqual(receipt.status, "delivered")
        self.assertEqual(receipt.attempt_id, 1)

    def test_required_identifiers_must_be_non_empty(self):
        for field in ("provider", "provider_event_id", "provider_message_id"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    self.make_receipt(**{field: " "})

    def test_unknown_status_is_rejected(self):
        with self.assertRaises(ValueError):
            self.make_receipt(status="opened")

    def test_timestamp_must_be_datetime_and_timezone_aware(self):
        with self.assertRaises(TypeError):
            self.make_receipt(provider_event_at="2026-11-19T00:00:00Z")
        with self.assertRaises(ValueError):
            self.make_receipt(provider_event_at=datetime(2026, 11, 19))

    def test_attempt_id_must_be_positive_integer_when_supplied(self):
        for value in (0, -1, True, "1"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.make_receipt(attempt_id=value)

    def test_attempt_id_can_be_absent_before_correlation(self):
        receipt = self.make_receipt(attempt_id=None)
        self.assertIsNone(receipt.attempt_id)


if __name__ == "__main__":
    unittest.main()
