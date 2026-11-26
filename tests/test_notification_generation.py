import os
import sqlite3
import uuid
from datetime import datetime, timezone

import pytest

from database.db_connection import get_db_connection
from database.migrations import apply_migrations
from database.notification_channel_preference_repository import save_channel_preference
from database.notification_repository import get_pending_notifications
from notifications.generation import generate_notifications


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "stage9_step4.db"
    monkeypatch.setenv("INDIAPOST_DB_PATH", str(path))
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript("""
        CREATE TABLE users (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            external_user_id TEXT
        );
        CREATE TABLE user_shipments (
            user_shipment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            shipment_id INTEGER NOT NULL,
            monitoring_enabled INTEGER NOT NULL DEFAULT 1,
            monitoring_interval_hours INTEGER NOT NULL DEFAULT 6,
            next_check_at TEXT,
            monitoring_completed_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, shipment_id),
            FOREIGN KEY(user_id) REFERENCES users(user_id),
            FOREIGN KEY(shipment_id) REFERENCES shipments(shipment_id)
        );
        CREATE TABLE notifications (
            notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_shipment_id INTEGER NOT NULL,
            checkpoint_id INTEGER,
            notification_type TEXT NOT NULL,
            recipient TEXT NOT NULL,
            previous_status TEXT,
            new_status TEXT,
            notification_message TEXT,
            delivery_status TEXT NOT NULL DEFAULT 'pending',
            provider_message_id TEXT,
            idempotency_key TEXT NOT NULL UNIQUE,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            sent_at TEXT,
            last_attempt_at TEXT,
            error_message TEXT,
            FOREIGN KEY(user_shipment_id) REFERENCES user_shipments(user_shipment_id)
        );
    """)
    connection.commit()
    apply_migrations(connection)
    connection.execute("INSERT INTO users(user_id, external_user_id) VALUES (1, 'test-user')")
    connection.execute("""INSERT INTO shipments
        (shipment_id, tracking_number, carrier, provider, current_status, created_at, updated_at)
        VALUES (10, 'AB123456789IN', 'india-post', 'aftership', 'In Transit', '2026-11-16T10:00:00+00:00', '2026-11-16T10:00:00+00:00')""")
    connection.execute("""INSERT INTO user_shipments
        (user_shipment_id, user_id, shipment_id, monitoring_enabled, created_at)
        VALUES (20, 1, 10, 1, '2026-11-16T10:00:00+00:00')""")
    connection.commit()
    connection.close()
    return path


def event(previous="In Transit", new="Out for Delivery"):
    now = datetime.now(timezone.utc).isoformat()
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "shipment.status_changed",
        "schema_version": 1,
        "source": "test",
        "occurred_at": now,
        "recorded_at": now,
        "shipment": {"shipment_id": 10, "tracking_number": "AB123456789IN", "carrier": "india-post"},
        "change": {"previous_status": previous, "new_status": new},
    }


def test_generates_one_pending_notification_per_eligible_channel(db):
    verified = datetime.now(timezone.utc).isoformat()
    save_channel_preference(1, "email", "person@example.com", True, verified)
    save_channel_preference(1, "telegram", "123456789", True, verified)
    result = generate_notifications(event())
    assert result["queued_or_already_queued"] == 2
    assert result["eligible_subscriptions"] == 1
    rows = get_pending_notifications()
    assert {row["notification_type"] for row in rows} == {"email", "telegram"}
    assert all(row["delivery_status"] == "pending" for row in rows)


def test_repeated_event_does_not_create_duplicates(db):
    verified = datetime.now(timezone.utc).isoformat()
    save_channel_preference(1, "email", "person@example.com", True, verified)
    payload = event()
    first = generate_notifications(payload)
    second = generate_notifications(payload)
    assert first["queued_or_already_queued"] == 1
    assert second["queued_or_already_queued"] == 1
    assert len(get_pending_notifications()) == 1


def test_unverified_or_disabled_preferences_are_not_queued(db):
    save_channel_preference(1, "email", "person@example.com", False, None)
    result = generate_notifications(event())
    assert result["queued_or_already_queued"] == 0
    assert result["skipped_subscriptions"] == 1
    assert get_pending_notifications() == []


def test_disabled_shipment_subscription_is_not_queued(db):
    connection = get_db_connection()
    connection.execute("UPDATE user_shipments SET monitoring_enabled = 0 WHERE user_shipment_id = 20")
    connection.commit()
    connection.close()
    verified = datetime.now(timezone.utc).isoformat()
    save_channel_preference(1, "email", "person@example.com", True, verified)
    result = generate_notifications(event())
    assert result["eligible_subscriptions"] == 0
    assert result["queued_or_already_queued"] == 0


def test_no_status_change_produces_no_notifications(db):
    verified = datetime.now(timezone.utc).isoformat()
    save_channel_preference(1, "email", "person@example.com", True, verified)
    result = generate_notifications(event("In Transit", "In Transit"))
    assert result["status"] == "no_status_change"
    assert result["queued_or_already_queued"] == 0


def test_event_for_unmatched_shipment_produces_no_notifications(db):
    verified = datetime.now(timezone.utc).isoformat()
    save_channel_preference(1, "email", "person@example.com", True, verified)
    payload = event()
    payload["shipment"]["shipment_id"] = 999
    result = generate_notifications(payload)
    assert result["eligible_subscriptions"] == 0
    assert result["queued_or_already_queued"] == 0


def test_malformed_event_is_rejected(db):
    payload = event()
    payload["unexpected"] = "not allowed"
    with pytest.raises(ValueError):
        generate_notifications(payload)
