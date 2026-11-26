import unittest
from notifications.delivery import AdapterRegistry, DeliveryResult
from notifications.telegram_adapter import TelegramAdapter


class FakeTelegramTransport:
    def __init__(self, response=None, error=None):
        self.response = response if response is not None else {"ok": True, "result": {"message_id": 123}}
        self.error = error
        self.calls = []

    def send_message(self, chat_id, text):
        self.calls.append((chat_id, text))
        if self.error:
            raise self.error
        return self.response


def valid_notification(**changes):
    value = {"notification_type": "telegram", "recipient": "123456789", "notification_message": "Shipment delivered."}
    value.update(changes)
    return value


class DeliveryContractTests(unittest.TestCase):
    def test_result_rejects_unknown_status(self):
        with self.assertRaises(ValueError):
            DeliveryResult("queued")

    def test_registry_registers_and_resolves_adapter(self):
        adapter = TelegramAdapter(FakeTelegramTransport())
        registry = AdapterRegistry()
        registry.register(adapter)
        self.assertIs(registry.get("telegram"), adapter)
        self.assertEqual(registry.channels(), ("telegram",))

    def test_registry_rejects_duplicate_channel(self):
        registry = AdapterRegistry()
        registry.register(TelegramAdapter(FakeTelegramTransport()))
        with self.assertRaises(ValueError):
            registry.register(TelegramAdapter(FakeTelegramTransport()))

    def test_registry_does_not_fallback_for_missing_channel(self):
        with self.assertRaises(LookupError):
            AdapterRegistry().get("email")


class TelegramAdapterTests(unittest.TestCase):
    def test_success_returns_provider_message_id(self):
        transport = FakeTelegramTransport()
        result = TelegramAdapter(transport).deliver(valid_notification())
        self.assertEqual(result.status, "sent")
        self.assertEqual(result.provider_message_id, "123")
        self.assertEqual(transport.calls, [("123456789", "Shipment delivered.")])

    def test_rejects_non_telegram_notification_without_transport_call(self):
        transport = FakeTelegramTransport()
        result = TelegramAdapter(transport).deliver(valid_notification(notification_type="email"))
        self.assertEqual(result.error_code, "channel_mismatch")
        self.assertEqual(transport.calls, [])

    def test_rejects_invalid_recipient_without_transport_call(self):
        transport = FakeTelegramTransport()
        result = TelegramAdapter(transport).deliver(valid_notification(recipient="@username"))
        self.assertEqual(result.error_code, "invalid_recipient")
        self.assertEqual(transport.calls, [])

    def test_rejects_empty_message_without_transport_call(self):
        transport = FakeTelegramTransport()
        result = TelegramAdapter(transport).deliver(valid_notification(notification_message=" "))
        self.assertEqual(result.error_code, "empty_message")
        self.assertEqual(transport.calls, [])

    def test_timeout_is_retryable(self):
        result = TelegramAdapter(FakeTelegramTransport(error=TimeoutError())).deliver(valid_notification())
        self.assertEqual(result.status, "retryable")
        self.assertEqual(result.error_code, "transport_timeout")

    def test_connection_error_is_retryable(self):
        result = TelegramAdapter(FakeTelegramTransport(error=ConnectionError())).deliver(valid_notification())
        self.assertEqual(result.status, "retryable")

    def test_temporary_provider_error_is_retryable(self):
        transport = FakeTelegramTransport(response={"ok": False, "error_code": 429})
        result = TelegramAdapter(transport).deliver(valid_notification())
        self.assertEqual(result.status, "retryable")
        self.assertEqual(result.error_code, "telegram_429")

    def test_permanent_provider_rejection_is_failed(self):
        transport = FakeTelegramTransport(response={"ok": False, "error_code": 400})
        result = TelegramAdapter(transport).deliver(valid_notification())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "telegram_rejected")

    def test_missing_provider_message_id_is_retryable(self):
        transport = FakeTelegramTransport(response={"ok": True, "result": {}})
        result = TelegramAdapter(transport).deliver(valid_notification())
        self.assertEqual(result.status, "retryable")
        self.assertEqual(result.error_code, "missing_message_id")

    def test_unexpected_exception_does_not_leak_exception_text(self):
        transport = FakeTelegramTransport(error=RuntimeError("secret-token-value"))
        result = TelegramAdapter(transport).deliver(valid_notification())
        self.assertEqual(result.status, "retryable")
        self.assertNotIn("secret-token-value", result.error_message)


if __name__ == "__main__":
    unittest.main()
