import unittest

from notifications.delivery import AdapterRegistry, DeliveryResult
from notifications.channel_readiness import get_channel_readiness


class FakeAdapter:
    def __init__(self, channel):
        self.channel = channel

    def deliver(self, notification):
        return DeliveryResult("sent", provider_message_id="mock")


class ChannelReadinessTests(unittest.TestCase):
    def test_empty_registry_reports_all_channels_unavailable(self):
        report = get_channel_readiness(AdapterRegistry())
        self.assertEqual(report["ready_channels"], [])
        self.assertEqual(
            report["unavailable_channels"],
            ["email", "sms", "telegram", "whatsapp"],
        )
        self.assertFalse(report["live_provider_health_checked"])

    def test_registered_and_configured_telegram_is_ready(self):
        registry = AdapterRegistry()
        registry.register(FakeAdapter("telegram"))
        report = get_channel_readiness(
            registry, configured_channels={"telegram"}
        )
        self.assertEqual(report["ready_channels"], ["telegram"])
        self.assertEqual(
            report["channels"]["telegram"]["status"], "ready"
        )
        for channel in ("email", "sms", "whatsapp"):
            self.assertFalse(report["channels"][channel]["ready"])

    def test_registration_without_configuration_is_not_ready(self):
        registry = AdapterRegistry()
        registry.register(FakeAdapter("telegram"))
        report = get_channel_readiness(registry)
        details = report["channels"]["telegram"]
        self.assertTrue(details["adapter_registered"])
        self.assertFalse(details["configuration_validated"])
        self.assertEqual(details["status"], "configuration_not_validated")

    def test_configuration_without_adapter_is_not_ready(self):
        report = get_channel_readiness(
            AdapterRegistry(), configured_channels={"email"}
        )
        details = report["channels"]["email"]
        self.assertFalse(details["adapter_registered"])
        self.assertTrue(details["configuration_validated"])
        self.assertEqual(details["status"], "adapter_not_registered")

    def test_unknown_configured_channel_is_rejected(self):
        with self.assertRaises(ValueError):
            get_channel_readiness(AdapterRegistry(), configured_channels={"carrier_pigeon"})

    def test_registry_without_channels_method_is_rejected(self):
        with self.assertRaises(TypeError):
            get_channel_readiness(object())

    def test_string_is_not_accepted_as_channel_collection(self):
        with self.assertRaises(TypeError):
            get_channel_readiness(AdapterRegistry(), configured_channels="telegram")


if __name__ == "__main__":
    unittest.main()
