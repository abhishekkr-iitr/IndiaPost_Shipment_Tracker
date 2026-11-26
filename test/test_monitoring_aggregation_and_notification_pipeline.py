"""Comprehensive tests for multi-owner monitoring aggregation, common outbox generation,
and fail-closed delivery authorization.
"""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from database import schema
from database.db_connection import get_db_connection
from database.migrations import apply_migrations
from database.monitoring_job_repository import (
    sync_monitoring_job,
    get_monitoring_job,
)
from database.user_shipment_repository import (
    save_user_shipment,
)
from database.guest_notification_repository import (
    create_guest_notification_identity,
    revoke_guest_notification_identity,
    create_guest_shipment_subscription,
    update_guest_monitoring_status,
    save_guest_channel_preference,
    revoke_guest_channel_consent,
)
from database.notification_channel_preference_repository import (
    save_channel_preference,
)
from database.notification_delivery_repository import (
    is_notification_authorized,
)
from notifications.delivery import DeliveryResult
from notifications.generation import (
    generate_notifications,
    generate_notifications_in_transaction,
)
from notifications.worker import NotificationWorker


class MockAdapter:
    def __init__(self):
        self.delivered_records = []

    def deliver(self, notification):
        self.delivered_records.append(notification)
        return DeliveryResult("sent", provider_message_id="mock_msg_123")


class MockRegistry:
    def __init__(self, adapter):
        self.adapter = adapter

    def get(self, channel):
        return self.adapter


class MonitoringAggregationAndNotificationPipelineTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_pipeline.db"
        self.prev_env = os.environ.get("INDIAPOST_DB_PATH")
        os.environ["INDIAPOST_DB_PATH"] = str(self.db_path)

        conn = get_db_connection()
        try:
            schema.create_users_table(connection=conn)
            schema.create_shipments_table(connection=conn)
            schema.create_user_shipments_table(connection=conn)
            schema.create_checkpoints_table(connection=conn)
            schema.create_tracking_checks_table(connection=conn)
            schema.create_notification_preferences_table(connection=conn)
            schema.create_notifications_table(connection=conn)
            apply_migrations(conn)

            # Seed test shipment
            conn.execute(
                """INSERT INTO shipments (
                    shipment_id, tracking_number, carrier, provider,
                    current_status, created_at, updated_at
                ) VALUES (
                    10, 'IN123456789XX', 'India Post', 'aftership',
                    'In Transit', '2026-11-20T10:00:00Z', '2026-11-20T10:00:00Z'
                )"""
            )
            # Seed test user
            conn.execute(
                """INSERT INTO users (user_id, external_user_id, created_at)
                   VALUES (1, 'u_primary', '2026-11-20T10:00:00Z')"""
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        if self.prev_env is not None:
            os.environ["INDIAPOST_DB_PATH"] = self.prev_env
        else:
            os.environ.pop("INDIAPOST_DB_PATH", None)
        self.temp_dir.cleanup()

    # -----------------------------------------------------------------------
    # Multi-Owner Monitoring Aggregation (Workstream 7)
    # -----------------------------------------------------------------------

    def test_multi_owner_monitoring_aggregation(self):
        # 1. Registered user subscribes
        user_sub_id = save_user_shipment(user_id=1, shipment_id=10)
        job1 = get_monitoring_job(10)
        self.assertIsNotNone(job1)
        self.assertEqual(job1["status"], "scheduled")

        # 2. Guest subscribes to the same shipment
        gid = create_guest_notification_identity()
        guest_sub_id = create_guest_shipment_subscription(gid, 10, monitoring_interval_hours=4)
        job2 = get_monitoring_job(10)
        self.assertEqual(job2["status"], "scheduled")

        # 3. Registered user disables monitoring -> Job remains scheduled because guest is active
        save_user_shipment(user_id=1, shipment_id=10, monitoring_enabled=0)
        job3 = get_monitoring_job(10)
        self.assertEqual(job3["status"], "scheduled")

        # 4. Guest also disables monitoring -> All owners disabled -> Job becomes disabled
        update_guest_monitoring_status(guest_sub_id, False)
        job4 = get_monitoring_job(10)
        self.assertEqual(job4["status"], "disabled")

        # 5. Re-enabling guest reactivates the monitoring job
        update_guest_monitoring_status(guest_sub_id, True)
        job5 = get_monitoring_job(10)
        self.assertEqual(job5["status"], "scheduled")

    # -----------------------------------------------------------------------
    # Dual-Ownership Notification Generation (Workstream 8)
    # -----------------------------------------------------------------------

    def test_dual_ownership_notification_generation(self):
        now_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # Setup registered subscriber with email
        user_sub_id = save_user_shipment(user_id=1, shipment_id=10)
        save_channel_preference(1, "email", "reg_user@example.com", enabled=True, verified_at=now_ts)

        # Setup guest subscriber with telegram
        gid = create_guest_notification_identity()
        guest_sub_id = create_guest_shipment_subscription(gid, 10)
        save_guest_channel_preference(gid, "telegram", "987654321", enabled=True, verified_at=now_ts, consent_at=now_ts)

        # Shipment change event payload
        event_payload = {
            "event_id": "evt_status_change_001",
            "event_type": "shipment.status_changed",
            "schema_version": 2,
            "source": "monitoring",
            "occurred_at": now_ts,
            "recorded_at": now_ts,
            "shipment": {
                "shipment_id": 10,
                "tracking_number": "IN123456789XX",
                "carrier": "India Post",
            },
            "change": {
                "previous_status": "In Transit",
                "new_status": "Out for Delivery",
            },
        }

        # Generate notifications
        gen_result = generate_notifications(event_payload)
        self.assertEqual(gen_result["status"], "generated")
        self.assertEqual(gen_result["eligible_subscriptions"], 2)
        self.assertEqual(gen_result["queued_or_already_queued"], 2)

        # Verify notifications in database
        conn = get_db_connection()
        try:
            rows = conn.execute(
                """SELECT notification_id, user_shipment_id, guest_subscription_id,
                          notification_type, recipient, delivery_status
                   FROM notifications ORDER BY notification_id"""
            ).fetchall()
            self.assertEqual(len(rows), 2)

            # Row 1: Registered owner
            self.assertEqual(rows[0][1], user_sub_id)
            self.assertIsNone(rows[0][2])
            self.assertEqual(rows[0][3], "email")
            self.assertEqual(rows[0][4], "reg_user@example.com")

            # Row 2: Guest owner
            self.assertIsNone(rows[1][1])
            self.assertEqual(rows[1][2], guest_sub_id)
            self.assertEqual(rows[1][3], "telegram")
            self.assertEqual(rows[1][4], "987654321")
        finally:
            conn.close()

        # Idempotency check: repeated event generation does not duplicate
        repeat_result = generate_notifications(event_payload)
        self.assertEqual(repeat_result["queued_or_already_queued"], 2)
        conn = get_db_connection()
        try:
            count = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
            self.assertEqual(count, 2)
        finally:
            conn.close()

    def test_transactional_notification_generation(self):
        now_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        user_sub_id = save_user_shipment(user_id=1, shipment_id=10)
        save_channel_preference(1, "email", "tx_user@example.com", enabled=True, verified_at=now_ts)

        gid = create_guest_notification_identity()
        guest_sub_id = create_guest_shipment_subscription(gid, 10)
        save_guest_channel_preference(gid, "telegram", "11223344", enabled=True, verified_at=now_ts, consent_at=now_ts)

        event_payload = {
            "event_id": "evt_tx_002",
            "event_type": "shipment.status_changed",
            "schema_version": 2,
            "source": "webhook",
            "occurred_at": now_ts,
            "recorded_at": now_ts,
            "shipment": {
                "shipment_id": 10,
                "tracking_number": "IN123456789XX",
                "carrier": "India Post",
            },
            "change": {
                "previous_status": "Out for Delivery",
                "new_status": "Delivered",
            },
        }

        conn = get_db_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            res = generate_notifications_in_transaction(conn, event_payload)
            conn.commit()
            self.assertEqual(res["eligible_subscriptions"], 2)
            self.assertEqual(res["queued_or_already_queued"], 2)
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Fail-Closed Final Delivery Authorization & Worker (Workstream 9)
    # -----------------------------------------------------------------------

    def test_fail_closed_delivery_authorization_lifecycle(self):
        now_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # Setup registered & guest subscriptions
        user_sub_id = save_user_shipment(user_id=1, shipment_id=10)
        save_channel_preference(1, "email", "authorizer@example.com", enabled=True, verified_at=now_ts)

        gid = create_guest_notification_identity()
        guest_sub_id = create_guest_shipment_subscription(gid, 10)
        save_guest_channel_preference(gid, "telegram", "55667788", enabled=True, verified_at=now_ts, consent_at=now_ts)

        # Enqueue notifications
        event_payload = {
            "event_id": "evt_auth_test",
            "event_type": "shipment.status_changed",
            "schema_version": 2,
            "source": "monitoring",
            "occurred_at": now_ts,
            "recorded_at": now_ts,
            "shipment": {
                "shipment_id": 10,
                "tracking_number": "IN123456789XX",
                "carrier": "India Post",
            },
            "change": {
                "previous_status": "In Transit",
                "new_status": "Out for Delivery",
            },
        }
        generate_notifications(event_payload)

        conn = get_db_connection()
        try:
            reg_notif_id = conn.execute("SELECT notification_id FROM notifications WHERE user_shipment_id IS NOT NULL").fetchone()[0]
            guest_notif_id = conn.execute("SELECT notification_id FROM notifications WHERE guest_subscription_id IS NOT NULL").fetchone()[0]
        finally:
            conn.close()

        # Both initially authorized
        self.assertTrue(is_notification_authorized(reg_notif_id))
        self.assertTrue(is_notification_authorized(guest_notif_id))

        # 1. Revoking guest consent immediately unauthorized the queued guest notification
        revoke_guest_channel_consent(gid, "telegram")
        self.assertFalse(is_notification_authorized(guest_notif_id))

        # Re-enabling consent restores authorization
        save_guest_channel_preference(gid, "telegram", "55667788", enabled=True, verified_at=now_ts, consent_at=now_ts)
        self.assertTrue(is_notification_authorized(guest_notif_id))

        # 2. Revoking guest identity immediately unauthorized the queued guest notification
        revoke_guest_notification_identity(gid)
        self.assertFalse(is_notification_authorized(guest_notif_id))

        # 3. Disabling registered monitoring unauthorizes registered notification
        save_user_shipment(user_id=1, shipment_id=10, monitoring_enabled=0)
        self.assertFalse(is_notification_authorized(reg_notif_id))

    def test_worker_dispatches_mixed_ownership_notifications(self):
        now_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save_user_shipment(user_id=1, shipment_id=10)
        save_channel_preference(1, "email", "worker_user@example.com", enabled=True, verified_at=now_ts)

        gid = create_guest_notification_identity()
        create_guest_shipment_subscription(gid, 10)
        save_guest_channel_preference(gid, "telegram", "99887766", enabled=True, verified_at=now_ts, consent_at=now_ts)

        event_payload = {
            "event_id": "evt_worker_test",
            "event_type": "shipment.status_changed",
            "schema_version": 2,
            "source": "monitoring",
            "occurred_at": now_ts,
            "recorded_at": now_ts,
            "shipment": {
                "shipment_id": 10,
                "tracking_number": "IN123456789XX",
                "carrier": "India Post",
            },
            "change": {
                "previous_status": "In Transit",
                "new_status": "Out for Delivery",
            },
        }
        generate_notifications(event_payload)

        mock_adapter = MockAdapter()
        worker = NotificationWorker("test_worker_1", MockRegistry(mock_adapter))
        counts = worker.run_once()

        self.assertEqual(counts["claimed"], 2)
        self.assertEqual(counts["sent"], 2)
        self.assertEqual(counts["failed"], 0)


if __name__ == "__main__":
    unittest.main()
