import json
import os
import tempfile
import unittest
import uuid

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


class TestWebhookEventProcessing(unittest.TestCase):

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()

        self.database_path = (
            Path(self.temp_directory.name) / "webhook_processing_test.db"
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
            apply_migrations(connection)
        finally:
            connection.close()

        from database.webhook_event_repository import (
            save_webhook_event,
            get_webhook_event,
            claim_next_webhook_event,
            mark_webhook_event_processed_in_transaction,
            mark_webhook_event_failed,
        )

        self.save_webhook_event = save_webhook_event
        self.get_webhook_event = get_webhook_event
        self.claim_next_webhook_event = claim_next_webhook_event
        self.mark_processed = (
            mark_webhook_event_processed_in_transaction
        )
        self.mark_failed = mark_webhook_event_failed

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

        saved = self.save_webhook_event(
            self.payload,
            self.raw_payload,
        )

        self.event_record_id = saved["event_record_id"]

    def tearDown(self):
        self.environment_patch.stop()
        self.temp_directory.cleanup()

    def _get_connection(self):
        from database.db_connection import get_db_connection
        return get_db_connection()

    def _make_retry_due(self):
        connection = self._get_connection()

        try:
            connection.execute(
                """
                UPDATE webhook_events
                SET next_retry_at = '2000-01-01T00:00:00+00:00'
                WHERE event_record_id = ?
                """,
                (self.event_record_id,),
            )
            connection.commit()
        finally:
            connection.close()

    def test_received_event_can_be_claimed(self):

        claimed = self.claim_next_webhook_event(
            worker_id="worker-1",
            lease_seconds=300,
        )

        self.assertIsNotNone(claimed)

        self.assertEqual(
            claimed["event_record_id"],
            self.event_record_id,
        )

        self.assertEqual(claimed["status"], "processing")
        self.assertEqual(claimed["claimed_by"], "worker-1")
        self.assertEqual(claimed["processing_attempts"], 1)

        self.assertIsNotNone(claimed["claimed_at"])
        self.assertIsNotNone(claimed["lease_expires_at"])

    def test_claimed_event_cannot_be_claimed_by_another_worker(self):

        first = self.claim_next_webhook_event(
            worker_id="worker-1",
        )

        second = self.claim_next_webhook_event(
            worker_id="worker-2",
        )

        self.assertIsNotNone(first)
        self.assertIsNone(second)

        record = self.get_webhook_event(
            self.event_record_id
        )

        self.assertEqual(record["claimed_by"], "worker-1")
        self.assertEqual(record["processing_attempts"], 1)

    def test_concurrent_workers_only_one_claims_event(self):

        worker_count = 8

        def claim(worker_number):

            return self.claim_next_webhook_event(
                worker_id=f"worker-{worker_number}",
            )

        with ThreadPoolExecutor(
            max_workers=worker_count
        ) as executor:

            futures = [
                executor.submit(claim, number)
                for number in range(worker_count)
            ]

            results = [
                future.result(timeout=30)
                for future in futures
            ]

        successful_claims = [
            result
            for result in results
            if result is not None
        ]

        self.assertEqual(len(successful_claims), 1)

        record = self.get_webhook_event(
            self.event_record_id
        )

        self.assertEqual(record["status"], "processing")
        self.assertEqual(record["processing_attempts"], 1)

    def test_expired_lease_can_be_reclaimed(self):

        first = self.claim_next_webhook_event(
            worker_id="worker-1",
            lease_seconds=300,
        )

        self.assertIsNotNone(first)

        connection = self._get_connection()

        try:
            connection.execute(
                """
                UPDATE webhook_events
                SET lease_expires_at =
                    '2000-01-01T00:00:00+00:00'
                WHERE event_record_id = ?
                """,
                (self.event_record_id,),
            )

            connection.commit()

        finally:
            connection.close()

        second = self.claim_next_webhook_event(
            worker_id="worker-2",
            lease_seconds=300,
        )

        self.assertIsNotNone(second)

        self.assertEqual(second["claimed_by"], "worker-2")
        self.assertEqual(second["processing_attempts"], 2)

    def test_unexpired_lease_cannot_be_reclaimed(self):

        first = self.claim_next_webhook_event(
            worker_id="worker-1",
            lease_seconds=300,
        )

        self.assertIsNotNone(first)

        second = self.claim_next_webhook_event(
            worker_id="worker-2",
        )

        self.assertIsNone(second)

    def test_failure_schedules_retry(self):

        claimed = self.claim_next_webhook_event(
            worker_id="worker-1",
        )

        result = self.mark_failed(
            event_record_id=self.event_record_id,
            worker_id="worker-1",
            error="Synthetic processing failure",
        )

        self.assertEqual(result["status"], "retryable")
        self.assertEqual(result["processing_attempts"], 1)
        self.assertIsNotNone(result["next_retry_at"])

        record = self.get_webhook_event(
            self.event_record_id
        )

        self.assertEqual(record["status"], "retryable")
        self.assertEqual(record["claimed_by"], None)
        self.assertEqual(record["claimed_at"], None)
        self.assertEqual(record["lease_expires_at"], None)
        self.assertEqual(
            record["last_error"],
            "Synthetic processing failure",
        )

    def test_retryable_event_is_not_claimed_before_retry_time(self):

        self.claim_next_webhook_event(
            worker_id="worker-1",
        )

        self.mark_failed(
            event_record_id=self.event_record_id,
            worker_id="worker-1",
            error="Temporary failure",
        )

        claimed = self.claim_next_webhook_event(
            worker_id="worker-2",
        )

        self.assertIsNone(claimed)

    def test_retryable_event_can_be_claimed_after_retry_time(self):

        self.claim_next_webhook_event(
            worker_id="worker-1",
        )

        self.mark_failed(
            event_record_id=self.event_record_id,
            worker_id="worker-1",
            error="Temporary failure",
        )

        self._make_retry_due()

        claimed = self.claim_next_webhook_event(
            worker_id="worker-2",
        )

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["status"], "processing")
        self.assertEqual(claimed["claimed_by"], "worker-2")
        self.assertEqual(claimed["processing_attempts"], 2)

    def test_five_failed_attempts_move_event_to_dead_letter(self):

        for attempt in range(1, 6):

            worker_id = f"worker-{attempt}"

            claimed = self.claim_next_webhook_event(
                worker_id=worker_id,
            )

            if attempt > 1:
                self.assertIsNotNone(claimed)

            self.assertIsNotNone(claimed)

            result = self.mark_failed(
                event_record_id=self.event_record_id,
                worker_id=worker_id,
                error=f"Synthetic failure {attempt}",
            )

            if attempt < 5:
                self.assertEqual(
                    result["status"],
                    "retryable",
                )

                self._make_retry_due()

            else:
                self.assertEqual(
                    result["status"],
                    "dead_letter",
                )

        record = self.get_webhook_event(
            self.event_record_id
        )

        self.assertEqual(record["status"], "dead_letter")
        self.assertEqual(record["processing_attempts"], 5)
        self.assertIsNone(record["next_retry_at"])
        self.assertIsNone(record["claimed_by"])

        further_claim = self.claim_next_webhook_event(
            worker_id="worker-6",
        )

        self.assertIsNone(further_claim)

    def test_expired_lease_after_fifth_attempt_moves_event_to_dead_letter(self):

        # Complete four failed attempts, making each retry immediately due.
        for attempt in range(1, 5):

            worker_id = f"worker-{attempt}"

            claimed = self.claim_next_webhook_event(
                worker_id=worker_id,
            )

            self.assertIsNotNone(claimed)
            self.assertEqual(
                claimed["processing_attempts"],
                attempt,
            )

            result = self.mark_failed(
                event_record_id=self.event_record_id,
                worker_id=worker_id,
                error=f"Synthetic failure {attempt}",
            )

            self.assertEqual(result["status"], "retryable")

            self._make_retry_due()

        # Claim the fifth and final permitted attempt.
        fifth_claim = self.claim_next_webhook_event(
            worker_id="worker-5",
            lease_seconds=300,
        )

        self.assertIsNotNone(fifth_claim)
        self.assertEqual(fifth_claim["processing_attempts"], 5)
        self.assertEqual(fifth_claim["claimed_by"], "worker-5")

        # Simulate worker-5 crashing without reporting failure.
        connection = self._get_connection()

        try:
            connection.execute(
                """
                UPDATE webhook_events
                SET lease_expires_at = '2000-01-01T00:00:00+00:00'
                WHERE event_record_id = ?
                """,
                (self.event_record_id,),
            )

            connection.commit()

        finally:
            connection.close()

        # A subsequent worker invocation must recover the abandoned event.
        sixth_claim = self.claim_next_webhook_event(
            worker_id="worker-6",
        )

        self.assertIsNone(sixth_claim)

        # Verify that recovery moved the event to dead_letter.
        record = self.get_webhook_event(
            self.event_record_id
        )

        self.assertEqual(record["status"], "dead_letter")
        self.assertEqual(record["processing_attempts"], 5)

        self.assertIsNone(record["claimed_by"])
        self.assertIsNone(record["claimed_at"])
        self.assertIsNone(record["lease_expires_at"])
        self.assertIsNone(record["next_retry_at"])

        self.assertIn(
            "lease expired",
            record["last_error"].lower(),
        )

        self.assertIn(
            "maximum processing attempts",
            record["last_error"].lower(),
        )

    def test_failure_from_wrong_worker_is_rejected(self):

        self.claim_next_webhook_event(
            worker_id="worker-1",
        )

        with self.assertRaises(RuntimeError):

            self.mark_failed(
                event_record_id=self.event_record_id,
                worker_id="worker-2",
                error="Unauthorized failure update",
            )

        record = self.get_webhook_event(
            self.event_record_id
        )

        self.assertEqual(record["status"], "processing")
        self.assertEqual(record["claimed_by"], "worker-1")

    def test_processed_event_requires_correct_worker(self):

        self.claim_next_webhook_event(
            worker_id="worker-1",
        )

        connection = self._get_connection()

        try:

            with self.assertRaises(RuntimeError):

                self.mark_processed(
                    connection,
                    self.event_record_id,
                    "worker-2",
                )

            connection.rollback()

        finally:
            connection.close()

        record = self.get_webhook_event(
            self.event_record_id
        )

        self.assertEqual(record["status"], "processing")

    def test_processed_event_does_not_commit_caller_transaction(self):

        self.claim_next_webhook_event(
            worker_id="worker-1",
        )

        connection = self._get_connection()

        try:

            result = self.mark_processed(
                connection,
                self.event_record_id,
                "worker-1",
            )

            self.assertTrue(result)
            self.assertTrue(connection.in_transaction)

            connection.rollback()

        finally:
            connection.close()

        record = self.get_webhook_event(
            self.event_record_id
        )

        self.assertEqual(record["status"], "processing")

    def test_processed_event_is_persisted_after_caller_commit(self):

        self.claim_next_webhook_event(
            worker_id="worker-1",
        )

        connection = self._get_connection()

        try:

            self.mark_processed(
                connection,
                self.event_record_id,
                "worker-1",
            )

            connection.commit()

        finally:
            connection.close()

        record = self.get_webhook_event(
            self.event_record_id
        )

        self.assertEqual(record["status"], "processed")
        self.assertIsNotNone(record["processed_at"])
        self.assertIsNone(record["claimed_by"])
        self.assertIsNone(record["lease_expires_at"])

    def test_event_cannot_be_processed_twice(self):

        self.claim_next_webhook_event(
            worker_id="worker-1",
        )

        connection = self._get_connection()

        try:

            self.mark_processed(
                connection,
                self.event_record_id,
                "worker-1",
            )

            connection.commit()

            with self.assertRaises(RuntimeError):

                self.mark_processed(
                    connection,
                    self.event_record_id,
                    "worker-1",
                )

        finally:
            connection.close()

        record = self.get_webhook_event(
            self.event_record_id
        )

        self.assertEqual(record["status"], "processed")


if __name__ == "__main__":
    unittest.main()