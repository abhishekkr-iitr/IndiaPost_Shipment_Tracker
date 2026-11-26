import httpx
import unittest

from notifications.telegram_transport import TelegramHTTPTransport
from notifications.telegram_adapter import TelegramAdapter


class TelegramHTTPTransportTests(unittest.TestCase):
    def test_send_message_posts_expected_payload_and_returns_provider_response(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["payload"] = request.read().decode("utf-8")
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 321}})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        transport = TelegramHTTPTransport("123:secret", client=client)
        try:
            result = transport.send_message("987654321", "Shipment update")
        finally:
            client.close()

        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["message_id"], 321)
        self.assertIn("/bot123:secret/sendMessage", seen["url"])
        self.assertEqual(seen["payload"], '{"chat_id":"987654321","text":"Shipment update"}')

    def test_http_429_is_returned_as_provider_failure(self):
        client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(429, json={"ok": False, "error_code": 429})
        ))
        transport = TelegramHTTPTransport("123:secret", client=client)
        try:
            result = transport.send_message("123", "hello")
        finally:
            client.close()
        self.assertIs(result["ok"], False)
        self.assertEqual(result["error_code"], 429)

    def test_non_json_http_error_is_normalized(self):
        client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(503, text="temporary outage")
        ))
        transport = TelegramHTTPTransport("123:secret", client=client)
        try:
            result = transport.send_message("123", "hello")
        finally:
            client.close()
        self.assertIs(result["ok"], False)
        self.assertEqual(result["error_code"], 503)

    def test_timeout_is_sanitized_for_adapter_retry_classification(self):
        def handler(request):
            raise httpx.ReadTimeout("timed out")
        client = httpx.Client(transport=httpx.MockTransport(handler))
        transport = TelegramHTTPTransport("123:secret", client=client)
        try:
            with self.assertRaises(TimeoutError) as caught:
                transport.send_message("123", "hello")
        finally:
            client.close()
        self.assertNotIn("secret", str(caught.exception))

    def test_connection_error_is_sanitized_for_adapter_retry_classification(self):
        def handler(request):
            raise httpx.ConnectError("connection failed")
        client = httpx.Client(transport=httpx.MockTransport(handler))
        transport = TelegramHTTPTransport("123:secret", client=client)
        try:
            with self.assertRaises(ConnectionError) as caught:
                transport.send_message("123", "hello")
        finally:
            client.close()
        self.assertNotIn("secret", str(caught.exception))

    def test_adapter_converts_transport_success_to_delivery_result(self):
        client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True, "result": {"message_id": 456}})
        ))
        transport = TelegramHTTPTransport("123:secret", client=client)
        try:
            adapter = TelegramAdapter(transport)
            result = adapter.deliver({
                "notification_type": "telegram",
                "recipient": "987654321",
                "notification_message": "Shipment delivered.",
            })
        finally:
            client.close()
        self.assertEqual(result.status, "sent")
        self.assertEqual(result.provider_message_id, "456")

    def test_invalid_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            TelegramHTTPTransport("")
        with self.assertRaises(ValueError):
            TelegramHTTPTransport("123:secret", timeout=0)


if __name__ == "__main__":
    unittest.main()
