import base64
import hashlib
import hmac
import json
import unittest
import uuid

from webhooks.webhook_security import (
    WebhookValidationError,
    validate_aftership_payload,
    verify_aftership_signature,
)


TEST_SECRET = "synthetic-test-secret"


def make_signature(body, secret=TEST_SECRET):
    digest = hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()

    return base64.b64encode(digest).decode("ascii")


def make_payload(**overrides):
    payload = {
        "ts": 1712741696,
        "event": "tracking_update",
        "event_id": str(uuid.uuid4()),
        "msg": {
            "id": "synthetic-tracking-id",
            "tracking_number": "TEST123456",
            "slug": "india-post",
            "tag": "InTransit",
            "subtag": "InTransit_001",
            "checkpoints": [],
        },
    }

    payload.update(overrides)

    return payload


class TestAfterShipSignature(unittest.TestCase):

    def test_valid_signature(self):
        body = b'{"event":"tracking_update"}'
        signature = make_signature(body)

        self.assertTrue(
            verify_aftership_signature(
                body, signature, TEST_SECRET
            )
        )

    def test_invalid_signature(self):
        body = b'{"event":"tracking_update"}'

        self.assertFalse(
            verify_aftership_signature(
                body, make_signature(body), "wrong-secret"
            )
        )

    def test_modified_body_rejected(self):
        body = b'{"event":"tracking_update"}'
        modified_body = b'{"event":"edd_revise"}'

        self.assertFalse(
            verify_aftership_signature(
                modified_body,
                make_signature(body),
                TEST_SECRET,
            )
        )

    def test_missing_signature_rejected(self):
        self.assertFalse(
            verify_aftership_signature(
                b'{"event":"tracking_update"}',
                "",
                TEST_SECRET,
            )
        )

    def test_malformed_base64_rejected(self):
        self.assertFalse(
            verify_aftership_signature(
                b'{"event":"tracking_update"}',
                "not-valid-base64!!!",
                TEST_SECRET,
            )
        )

    def test_empty_secret_rejected(self):
        body = b'{"event":"tracking_update"}'

        self.assertFalse(
            verify_aftership_signature(
                body,
                make_signature(body),
                "",
            )
        )

    def test_non_bytes_body_rejected(self):
        self.assertFalse(
            verify_aftership_signature(
                '{"event":"tracking_update"}',
                "signature",
                TEST_SECRET,
            )
        )

    def test_whitespace_changes_signature(self):
        original = b'{"event":"tracking_update"}'
        modified = b'{ "event":"tracking_update" }'

        self.assertFalse(
            verify_aftership_signature(
                modified,
                make_signature(original),
                TEST_SECRET,
            )
        )


class TestAfterShipPayloadValidation(unittest.TestCase):

    def test_valid_tracking_update(self):
        body = json.dumps(make_payload()).encode("utf-8")

        result = validate_aftership_payload(body)

        self.assertEqual(result["event"], "tracking_update")

    def test_valid_edd_revise_event(self):
        body = json.dumps(
            make_payload(event="edd_revise")
        ).encode("utf-8")

        result = validate_aftership_payload(body)

        self.assertEqual(result["event"], "edd_revise")

    def test_valid_tracking_pending_time_event(self):
        body = json.dumps(
            make_payload(event="tracking_pending_time")
        ).encode("utf-8")

        result = validate_aftership_payload(body)

        self.assertEqual(
            result["event"],
            "tracking_pending_time",
        )

    def test_missing_required_field(self):
        payload = make_payload()
        del payload["event_id"]

        body = json.dumps(payload).encode("utf-8")

        with self.assertRaises(WebhookValidationError):
            validate_aftership_payload(body)

    def test_invalid_json(self):
        with self.assertRaises(WebhookValidationError):
            validate_aftership_payload(b"{invalid-json")

    def test_empty_body(self):
        with self.assertRaises(WebhookValidationError):
            validate_aftership_payload(b"")

    def test_json_root_must_be_object(self):
        with self.assertRaises(WebhookValidationError):
            validate_aftership_payload(b"[]")

    def test_unsupported_event(self):
        payload = make_payload(event="unknown_event")
        body = json.dumps(payload).encode("utf-8")

        with self.assertRaises(WebhookValidationError):
            validate_aftership_payload(body)

    def test_invalid_event_id(self):
        payload = make_payload(event_id="not-a-uuid")
        body = json.dumps(payload).encode("utf-8")

        with self.assertRaises(WebhookValidationError):
            validate_aftership_payload(body)

    def test_non_v4_event_id(self):
        payload = make_payload(
            event_id=str(uuid.uuid1())
        )

        body = json.dumps(payload).encode("utf-8")

        with self.assertRaises(WebhookValidationError):
            validate_aftership_payload(body)

    def test_invalid_timestamp(self):
        payload = make_payload(ts="not-a-timestamp")
        body = json.dumps(payload).encode("utf-8")

        with self.assertRaises(WebhookValidationError):
            validate_aftership_payload(body)

    def test_boolean_timestamp_rejected(self):
        payload = make_payload(ts=True)
        body = json.dumps(payload).encode("utf-8")

        with self.assertRaises(WebhookValidationError):
            validate_aftership_payload(body)

    def test_msg_must_be_object(self):
        payload = make_payload(msg=[])
        body = json.dumps(payload).encode("utf-8")

        with self.assertRaises(WebhookValidationError):
            validate_aftership_payload(body)

    def test_optional_tracking_details_not_required(self):
        payload = make_payload(msg={})
        body = json.dumps(payload).encode("utf-8")

        result = validate_aftership_payload(body)

        self.assertEqual(result["msg"], {})


if __name__ == "__main__":
    unittest.main()