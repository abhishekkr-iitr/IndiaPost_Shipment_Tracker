"""Focused mocked tests for the local browser application API."""

import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app import app
from api import conversation_routes


class LocalApplicationAPITests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.create_patcher = patch(
            "api.conversation_routes.create_conversation",
            side_effect=lambda channel: "guest-conversation-001",
        )
        self.messages_patcher = patch(
            "api.conversation_routes.get_conversation_messages",
            return_value=[],
        )
        self.ask_patcher = patch(
            "api.conversation_routes.ask_openai",
            return_value="Mock assistant response.",
        )
        self.persist_patcher = patch("api.conversation_routes.persist_session")
        self.add_patcher = patch("api.conversation_routes.add_conversation")
        self.load_patcher = patch("api.conversation_routes.load_session", return_value={"active_conversation_id": "guest-conversation-001"})
        self.owned_patcher = patch("api.conversation_routes.is_owned_conversation", return_value=True)
        self.list_patcher = patch("api.conversation_routes.load_conversations", return_value=[])
        self.get_conversation_patcher = patch("api.conversation_routes.get_conversation", return_value={"status": "active"})
        self.select_patcher = patch("api.conversation_routes.set_active_conversation", return_value={"conversation_id": "guest-conversation-001", "status": "active"})
        self.close_patcher = patch("api.conversation_routes.close_conversation", return_value=True)
        self.create_patcher.start()
        self.messages_patcher.start()
        self.mock_ask = self.ask_patcher.start()
        self.persist_patcher.start()
        self.add_patcher.start()
        self.load_patcher.start()
        self.owned_patcher.start()
        self.list_patcher.start()
        self.get_conversation_patcher.start()
        self.select_patcher.start()
        self.close_patcher.start()

    def tearDown(self):
        self.ask_patcher.stop()
        self.messages_patcher.stop()
        self.create_patcher.stop()
        self.persist_patcher.stop()
        self.add_patcher.stop()
        self.load_patcher.stop()
        self.owned_patcher.stop()
        self.list_patcher.stop()
        self.get_conversation_patcher.stop()
        self.select_patcher.stop()
        self.close_patcher.stop()

    def test_session_creates_server_owned_guest_conversation(self):
        response = self.client.get("/api/session")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["conversation_id"], "guest-conversation-001")
        self.assertIn("httponly", response.headers.get("set-cookie", "").lower())
        self.assertNotIn("user_id", response.json())

    def test_message_uses_server_owned_conversation_and_guest_context(self):
        self.client.get("/api/session")
        request_id = str(uuid4())
        response = self.client.post(
            "/api/messages",
            json={"message": "Track my consignment", "request_id": request_id},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["response"], "Mock assistant response.")
        self.mock_ask.assert_called_once_with(
            "Track my consignment",
            conversation_id="guest-conversation-001",
            request_id=request_id,
            user_context=None,
        )

    def test_message_requires_established_session(self):
        response = self.client.post(
            "/api/messages",
            json={"message": "Hello", "request_id": str(uuid4())},
        )
        self.assertEqual(response.status_code, 401)
        self.mock_ask.assert_not_called()

    def test_invalid_request_id_is_rejected_before_ai_call(self):
        self.client.get("/api/session")
        response = self.client.post(
            "/api/messages",
            json={"message": "Hello", "request_id": "not-a-uuid"},
        )
        self.assertEqual(response.status_code, 422)
        self.mock_ask.assert_not_called()

    def test_cross_origin_write_is_rejected(self):
        self.client.get("/api/session")
        response = self.client.post(
            "/api/messages",
            headers={"Origin": "https://attacker.example"},
            json={"message": "Hello", "request_id": str(uuid4())},
        )
        self.assertEqual(response.status_code, 403)
        self.mock_ask.assert_not_called()

    def test_history_endpoint_returns_only_session_history(self):
        self.client.get("/api/session")
        response = self.client.get("/api/conversations")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"conversations": []})

    def test_open_conversation_requires_session_association(self):
        self.client.get("/api/session")
        with patch("api.conversation_routes.set_active_conversation", return_value=None):
            response = self.client.get("/api/conversations/not-owned")
        self.assertEqual(response.status_code, 404)

    def test_close_conversation_preserves_history_and_creates_new_active_chat(self):
        self.client.get("/api/session")
        with patch("api.conversation_routes.create_conversation", return_value="guest-conversation-002") as create:
            response = self.client.post("/api/conversations/guest-conversation-001/close", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["closed_conversation_id"], "guest-conversation-001")
        self.assertEqual(response.json()["conversation_id"], "guest-conversation-002")
        create.assert_called_once_with(channel="local_web")

    def test_health_endpoint_does_not_call_provider(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.mock_ask.assert_not_called()

    def test_index_serves_html_with_auth_and_alerts_ui(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("auth-status-btn", html)
        self.assertIn("open-alerts-btn", html)
        self.assertIn("auth-modal", html)
        self.assertIn("alerts-modal", html)
        self.assertIn("telegram-link-btn", html)

    def test_static_assets_are_served(self):
        css_res = self.client.get("/static/css/style.css")
        self.assertEqual(css_res.status_code, 200)
        self.assertIn(".topbar-btn", css_res.text)

        js_res = self.client.get("/static/js/app.js")
        self.assertEqual(js_res.status_code, 200)
        self.assertIn("checkAuthState", js_res.text)


if __name__ == "__main__":
    unittest.main()
