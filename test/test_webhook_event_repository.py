import json
import os
import tempfile
import unittest
import uuid

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path
from unittest.mock import patch


class TestWebhookEventRepository(unittest.TestCase):

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()

        self.database_path = (
            Path(self.temp_directory.name) / "webhook_test.db"
        )

        self.environment_patch = patch.dict(
            os.environ,
            {
                "INDIAPOST_DB_PATH": str(self.database_path)
            },
        )

        self.environment_patch.start()

        from database.db_connection import get_db_connection
        from database.migrations import apply_migrations

        connection = get_db_connection()

        try:
            self.migration_result = apply_migrations(connection)
        finally:
            connection.close()

        from database.webhook_event_repository import (
            save_webhook_event,
            get_webhook_event,
            get_webhook_event_by_provider_id,
            list_webhook_events,
        )

        self.save_webhook_event = save_webhook_event
        self.get_webhook_event = get_webhook_event
        self.get_webhook_event_by_provider_id = (
            get_webhook_event_by_provider_id
        )
        self.list_webhook_events = list_webhook_events

        self.payload = {
            "ts": 1794825600,
            "event": "tracking_update",
            "event_id": str(uuid.uuid4()),
            "msg": {
                "id": "synthetic-tracking-id",
                "tracking_number": "TEST123456",
                "slug": "india-post",
                "tag": "InTransit",
                "checkpoints": [],
            },
        }

        self.raw_payload = json.dumps(
            self.payload,
            separators=(",", ":"),
        ).encode("utf-8")

    def tearDown(self):
        self.environment_patch.stop()
        self.temp_directory.cleanup()

    def _create_listing_test_event(
        self,
        tracking_number,
        provider="aftership",
        status="received",
        received_at=None,
    ):
        from database.db_connection import get_db_connection

        payload = json.loads(
            json.dumps(self.payload)
        )

        payload["event_id"] = str(uuid.uuid4())
        payload["msg"]["tracking_number"] = tracking_number

        raw_payload = json.dumps(
            payload,
            separators=(",", ":"),
        ).encode("utf-8")

        result = self.save_webhook_event(
            payload,
            raw_payload,
            provider=provider,
        )

        if status != "received" or received_at is not None:
            connection = get_db_connection()

            try:
                updates = []
                parameters = []

                if status != "received":
                    updates.append("status = ?")
                    parameters.append(status)

                if received_at is not None:
                    updates.append("received_at = ?")
                    parameters.append(received_at)

                parameters.append(result["event_record_id"])

                connection.execute(
                    f"""
                    UPDATE webhook_events
                    SET {", ".join(updates)}
                    WHERE event_record_id = ?
                    """,
                    parameters,
                )

                connection.commit()

            finally:
                connection.close()

        return result["event_record_id"]


    def test_migration_applied(self):
        # Migration 4 adds channel-independent notification preferences.
        # Migration 5 is correctly deferred in this fixture because it does
        # not create the notifications table.
        self.assertEqual(
            self.migration_result["current_version"],
            4,
        )

        self.assertEqual(
            self.migration_result["applied"],
            [1, 2, 3, 4],
        )

    def test_save_webhook_event(self):
        result = self.save_webhook_event(
            self.payload,
            self.raw_payload,
        )

        self.assertTrue(result["inserted"])
        self.assertTrue(result["event_record_id"])

        record = self.get_webhook_event(
            result["event_record_id"]
        )

        self.assertIsNotNone(record)
        self.assertEqual(record["provider"], "aftership")
        self.assertEqual(
            record["provider_event_id"],
            self.payload["event_id"],
        )
        self.assertEqual(
            record["event_type"],
            "tracking_update",
        )
        self.assertEqual(record["status"], "received")
        self.assertEqual(record["processing_attempts"], 0)
        self.assertEqual(
            record["tracking_number"],
            "TEST123456",
        )
        self.assertEqual(record["carrier"], "india-post")
        self.assertEqual(
            record["raw_payload"],
            self.raw_payload.decode("utf-8"),
        )

    def test_duplicate_event_does_not_create_second_record(self):
        first = self.save_webhook_event(
            self.payload,
            self.raw_payload,
        )

        second = self.save_webhook_event(
            self.payload,
            self.raw_payload,
        )

        self.assertTrue(first["inserted"])
        self.assertFalse(second["inserted"])

        self.assertEqual(
            first["event_record_id"],
            second["event_record_id"],
        )

        from database.db_connection import get_db_connection

        connection = get_db_connection()

        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM webhook_events"
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(count, 1)

    def test_duplicate_event_does_not_overwrite_original_payload(self):
        first = self.save_webhook_event(
            self.payload,
            self.raw_payload,
        )

        modified_payload = json.loads(
            self.raw_payload.decode("utf-8")
        )

        modified_payload["msg"]["tag"] = "Delivered"
        modified_payload["msg"]["tracking_number"] = "MODIFIED123"

        modified_raw_payload = json.dumps(
            modified_payload,
            separators=(",", ":"),
        ).encode("utf-8")

        second = self.save_webhook_event(
            modified_payload,
            modified_raw_payload,
        )

        self.assertTrue(first["inserted"])
        self.assertFalse(second["inserted"])

        self.assertEqual(
            first["event_record_id"],
            second["event_record_id"],
        )

        record = self.get_webhook_event(
            first["event_record_id"]
        )

        self.assertEqual(
            record["raw_payload"],
            self.raw_payload.decode("utf-8"),
        )

        self.assertEqual(
            record["tracking_number"],
            "TEST123456",
        )

        self.assertEqual(record["status"], "received")


    def test_concurrent_duplicate_event_submissions_create_one_record(self):
        worker_count = 8
        barrier = Barrier(worker_count)

        def submit_event():
            barrier.wait(timeout=10)

            return self.save_webhook_event(
                self.payload,
                self.raw_payload,
            )

        with ThreadPoolExecutor(
            max_workers=worker_count
        ) as executor:

            futures = [
                executor.submit(submit_event)
                for _ in range(worker_count)
            ]

            results = [
                future.result(timeout=20)
                for future in futures
            ]

        inserted_count = sum(
            1 for result in results
            if result["inserted"]
        )

        self.assertEqual(inserted_count, 1)

        record_ids = {
            result["event_record_id"]
            for result in results
        }

        self.assertEqual(len(record_ids), 1)

        from database.db_connection import get_db_connection

        connection = get_db_connection()

        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM webhook_events"
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(count, 1)

    def test_provider_event_id_lookup(self):
        saved = self.save_webhook_event(
            self.payload,
            self.raw_payload,
        )

        record = self.get_webhook_event_by_provider_id(
            self.payload["event_id"]
        )

        self.assertIsNotNone(record)

        self.assertEqual(
            record["event_record_id"],
            saved["event_record_id"],
        )

    def test_same_event_id_from_different_provider_is_distinct(self):
        first = self.save_webhook_event(
            self.payload,
            self.raw_payload,
            provider="aftership",
        )

        second = self.save_webhook_event(
            self.payload,
            self.raw_payload,
            provider="another-provider",
        )

        self.assertTrue(first["inserted"])
        self.assertTrue(second["inserted"])

        self.assertNotEqual(
            first["event_record_id"],
            second["event_record_id"],
        )

    def test_invalid_payload_type_rejected(self):
        with self.assertRaises(ValueError):
            self.save_webhook_event(
                "not-a-dictionary",
                self.raw_payload,
            )

    def test_invalid_raw_payload_type_rejected(self):
        with self.assertRaises(ValueError):
            self.save_webhook_event(
                self.payload,
                "not-bytes",
            )

    def test_invalid_raw_json_rejected(self):
        with self.assertRaises(ValueError):
            self.save_webhook_event(
                self.payload,
                b"{invalid-json",
            )

    def test_missing_provider_event_id_rejected(self):
        payload = dict(self.payload)
        payload.pop("event_id")

        with self.assertRaises(ValueError):
            self.save_webhook_event(
                payload,
                self.raw_payload,
            )

    def test_missing_message_rejected(self):
        payload = dict(self.payload)
        payload["msg"] = None

        with self.assertRaises(ValueError):
            self.save_webhook_event(
                payload,
                self.raw_payload,
            )

    def test_missing_record_returns_none(self):
        self.assertIsNone(
            self.get_webhook_event(str(uuid.uuid4()))
        )

    def test_list_webhook_events_returns_all_events(self):
        self._create_listing_test_event("TRACK001")
        self._create_listing_test_event("TRACK002")
        self._create_listing_test_event("TRACK003")

        records = self.list_webhook_events()

        self.assertEqual(len(records), 3)

        self.assertEqual(
            {record["tracking_number"] for record in records},
            {"TRACK001", "TRACK002", "TRACK003"},
        )

    def test_list_webhook_events_filters_by_status(self):
        self._create_listing_test_event(
            "TRACK001",
            status="received",
        )

        self._create_listing_test_event(
            "TRACK002",
            status="dead_letter",
        )

        records = self.list_webhook_events(
            status="dead_letter"
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["tracking_number"], "TRACK002")

    def test_list_webhook_events_filters_by_provider(self):
        self._create_listing_test_event(
            "TRACK001",
            provider="aftership",
        )

        self._create_listing_test_event(
            "TRACK002",
            provider="another-provider",
        )

        records = self.list_webhook_events(
            provider="another-provider"
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["tracking_number"], "TRACK002")

    def test_list_webhook_events_filters_by_tracking_number(self):
        self._create_listing_test_event("TRACK001")
        self._create_listing_test_event("TRACK002")

        records = self.list_webhook_events(
            tracking_number="TRACK002"
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["tracking_number"], "TRACK002")

    def test_list_webhook_events_supports_combined_filters(self):
        self._create_listing_test_event(
            "TRACK001",
            provider="aftership",
            status="dead_letter",
        )

        self._create_listing_test_event(
            "TRACK002",
            provider="another-provider",
            status="dead_letter",
        )

        self._create_listing_test_event(
            "TRACK003",
            provider="aftership",
            status="received",
        )

        records = self.list_webhook_events(
            status="dead_letter",
            provider="aftership",
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["tracking_number"], "TRACK001")

    def test_list_webhook_events_orders_newest_first(self):
        self._create_listing_test_event(
            "TRACK001",
            received_at="2026-11-16T10:00:00+00:00",
        )

        self._create_listing_test_event(
            "TRACK002",
            received_at="2026-11-16T11:00:00+00:00",
        )

        self._create_listing_test_event(
            "TRACK003",
            received_at="2026-11-16T12:00:00+00:00",
        )

        records = self.list_webhook_events()

        self.assertEqual(
            [record["tracking_number"] for record in records],
            ["TRACK003", "TRACK002", "TRACK001"],
        )

    def test_list_webhook_events_supports_limit_and_offset(self):
        self._create_listing_test_event(
            "TRACK001",
            received_at="2026-11-16T10:00:00+00:00",
        )

        self._create_listing_test_event(
            "TRACK002",
            received_at="2026-11-16T11:00:00+00:00",
        )

        self._create_listing_test_event(
            "TRACK003",
            received_at="2026-11-16T12:00:00+00:00",
        )

        records = self.list_webhook_events(
            limit=1,
            offset=1,
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["tracking_number"], "TRACK002")

    def test_list_webhook_events_returns_empty_list_when_no_match(self):
        self._create_listing_test_event("TRACK001")

        records = self.list_webhook_events(
            status="dead_letter"
        )

        self.assertEqual(records, [])

    def test_list_webhook_events_rejects_invalid_limit(self):
        for invalid_limit in (0, -1, 501, True, 1.5, "10"):
            with self.subTest(limit=invalid_limit):
                with self.assertRaises(ValueError):
                    self.list_webhook_events(limit=invalid_limit)

    def test_list_webhook_events_rejects_invalid_offset(self):
        for invalid_offset in (-1, True, 1.5, "10"):
            with self.subTest(offset=invalid_offset):
                with self.assertRaises(ValueError):
                    self.list_webhook_events(offset=invalid_offset)

    def test_list_webhook_events_rejects_empty_filters(self):
        invalid_arguments = (
            {"status": ""},
            {"provider": ""},
            {"tracking_number": ""},
        )

        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    self.list_webhook_events(**arguments)

if __name__ == "__main__":
    unittest.main()