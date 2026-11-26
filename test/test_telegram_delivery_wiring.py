import unittest

import httpx

from notifications.delivery_setup import build_delivery_runtime


class TelegramDeliveryWiringTests(unittest.TestCase):
    def test_registry_wires_telegram_adapter_to_mocked_http_transport(self):
        seen = {}

        def handler(request):
            seen["method"] = request.method
            seen["path"] = request.url.path
            seen["payload"] = request.read().decode("utf-8")
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 789}})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with build_delivery_runtime(
            bot_token="123:mock-token",
            http_client=client,
        ) as runtime:
            self.assertEqual(runtime.registry.channels(), ("telegram",))
            adapter = runtime.registry.get("telegram")
            result = adapter.deliver({
                "notification_type": "telegram",
                "recipient": "987654321",
                "notification_message": "Mock shipment update",
            })

        client.close()
        self.assertEqual(result.status, "sent")
        self.assertEqual(result.provider_message_id, "789")
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["path"], "/bot123:mock-token/sendMessage")
        self.assertEqual(
            seen["payload"],
            '{"chat_id":"987654321","text":"Mock shipment update"}',
        )

    def test_registry_uses_environment_token(self):
        with build_delivery_runtime(environ={"TELEGRAM_BOT_TOKEN": "123:mock-token"}) as runtime:
            self.assertEqual(runtime.registry.channels(), ("telegram",))

    def test_missing_token_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "TELEGRAM_BOT_TOKEN"):
            build_delivery_runtime(environ={})

    def test_invalid_notification_is_rejected_without_http_request(self):
        calls = []
        client = httpx.Client(transport=httpx.MockTransport(
            lambda request: (calls.append(request), httpx.Response(
                200, json={"ok": True, "result": {"message_id": 1}}
            ))[1]
        ))
        with build_delivery_runtime(bot_token="123:mock-token", http_client=client) as runtime:
            result = runtime.registry.get("telegram").deliver({
                "notification_type": "email",
                "recipient": "987654321",
                "notification_message": "Must not be sent",
            })
        client.close()
        self.assertEqual(result.status, "failed")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
