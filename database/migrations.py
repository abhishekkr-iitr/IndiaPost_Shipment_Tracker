"""
Explicit, versioned SQLite schema migrations.

Run with:
    python -m database.migrations

The migration runner is deliberately not called at import time.
"""

from datetime import datetime, timezone

from database.db_connection import get_db_connection
from database import schema


LATEST_SCHEMA_VERSION = 11
CONVERSATION_TABLES = {
    "conversations",
    "conversation_messages",
    "conversation_requests",
    "conversation_tool_rounds",
    "conversation_tool_calls",
}

REQUIRED_COLUMNS = {
    "conversations": {
        "conversation_id", "user_id", "channel",
        "channel_conversation_id", "status", "provider",
        "provider_conversation_id", "created_at", "updated_at",
        "closed_at",
    },
    "conversation_messages": {
        "message_id", "conversation_id", "sequence_number", "role",
        "content", "tool_name", "tool_call_id",
        "provider_message_id", "created_at",
    },
    "conversation_requests": {
        "request_id", "conversation_id", "user_message_id",
        "response_message_id", "request_hash", "status", "error_code",
        "created_at", "updated_at", "completed_at",
    },
    "conversation_tool_rounds": {
        "round_id", "conversation_id", "request_id", "round_number",
        "status", "expected_call_count", "created_at", "updated_at",
        "completed_at",
    },
    "conversation_tool_calls": {
        "tool_execution_id", "conversation_id", "request_id", "round_id",
        "round_number", "tool_call_id", "tool_name", "arguments",
        "call_message_id", "output_message_id", "status", "error_code",
        "created_at", "started_at", "updated_at", "completed_at",
    },
}


class MigrationError(RuntimeError):
    """Raised when the database cannot be migrated safely."""


def _table_exists(connection, table_name):
    return connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table_name,),
    ).fetchone() is not None


def _conversation_tables_present(connection):
    return {
        name
        for name in CONVERSATION_TABLES
        if _table_exists(connection, name)
    }


def _validate_conversation_schema(connection):
    missing_tables = CONVERSATION_TABLES - _conversation_tables_present(
        connection
    )
    if missing_tables:
        raise MigrationError(
            "Conversation schema is incomplete; missing tables: "
            + ", ".join(sorted(missing_tables))
        )

    for table_name, required in REQUIRED_COLUMNS.items():
        actual = {
            row[1]
            for row in connection.execute(
                f'PRAGMA table_info("{table_name}")'
            ).fetchall()
        }
        missing_columns = required - actual
        if missing_columns:
            raise MigrationError(
                f"Table {table_name!r} is missing required columns: "
                + ", ".join(sorted(missing_columns))
            )


def _ensure_migration_table(connection):
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def _apply_conversation_persistence_schema(connection):
    """
    Apply the conversation persistence schema using the caller's connection.

    The functions in database.schema retain their existing no-argument
    behaviour. Passing this connection keeps all DDL inside this transaction.
    """
    schema.create_users_table(connection=connection)
    schema.create_conversations_table(connection=connection)
    schema.create_conversation_messages_table(connection=connection)
    schema.create_conversation_requests_table(connection=connection)
    schema.create_conversation_tool_rounds_table(connection=connection)

    # The current table creator also creates an index on round_id. For an
    # older existing table, add round_id before calling that creator so its
    # index statement can succeed. A fresh table already includes round_id.
    tool_calls_already_exist = _table_exists(
        connection, "conversation_tool_calls"
    )
    if tool_calls_already_exist:
        schema.migrate_conversation_tool_calls_add_round_id(
            connection=connection
        )

    schema.create_conversation_tool_calls_table(connection=connection)

    if not tool_calls_already_exist:
        schema.migrate_conversation_tool_calls_add_round_id(
            connection=connection
        )

    _validate_conversation_schema(connection)


def _apply_webhook_event_persistence_schema(connection):
    """Create durable storage for incoming webhook events."""

    connection.execute("""
        CREATE TABLE IF NOT EXISTS webhook_events (
            event_record_id TEXT PRIMARY KEY,

            provider TEXT NOT NULL,
            provider_event_id TEXT NOT NULL,
            event_type TEXT NOT NULL,

            provider_event_timestamp REAL NOT NULL,
            provider_tracking_id TEXT,
            tracking_number TEXT,
            carrier TEXT,

            raw_payload TEXT NOT NULL,
            received_at TEXT NOT NULL,

            status TEXT NOT NULL DEFAULT 'received'
                CHECK (
                    status IN (
                        'received',
                        'processing',
                        'processed',
                        'retryable',
                        'dead_letter'
                    )
                ),

            processing_attempts INTEGER NOT NULL DEFAULT 0
                CHECK (processing_attempts >= 0),

            next_retry_at TEXT,

            claimed_by TEXT,
            claimed_at TEXT,
            lease_expires_at TEXT,

            processed_at TEXT,
            last_error TEXT,

            UNIQUE (provider, provider_event_id)
        )
    """)

    connection.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_webhook_events_processing
        ON webhook_events(status, next_retry_at, received_at)
    """)

    connection.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_webhook_events_tracking
        ON webhook_events(tracking_number, carrier)
    """)

def _apply_webhook_event_ordering_schema(connection):
    """
    Add a dedicated provider-event timestamp watermark to shipments.

    This timestamp is distinct from last_checked_at and represents
    the latest provider event successfully applied to the shipment.
    """

    # Ensure the base shipments table exists.
    schema.create_shipments_table(connection=connection)

    existing_columns = {
        row[1]
        for row in connection.execute(
            'PRAGMA table_info("shipments")'
        ).fetchall()
    }

    if "last_provider_event_timestamp" not in existing_columns:
        connection.execute("""
            ALTER TABLE shipments
            ADD COLUMN last_provider_event_timestamp REAL
        """)

def apply_migrations(connection=None):
    """
    Apply all pending migrations in version order.

    Version 1: Conversation persistence.
    Version 2: Webhook event persistence.
    Version 3: Webhook event ordering.
    Version 4: Channel-independent notification preferences.
    Version 5: Durable notification delivery claims and retry scheduling.
    Version 6: Proactive shipment monitoring jobs.
    Version 7: Durable shipment change-event ledger.

    If connection is omitted, a connection is created and owned here.
    A supplied connection remains open and is not closed by this function.
    The caller must not pass a connection with an active transaction.
    """
    owns_connection = connection is None

    if owns_connection:
        connection = get_db_connection()

    migration_transaction_started = False

    try:
        if connection.in_transaction:
            raise MigrationError(
                "Migration requires a connection with no active transaction."
            )

        connection.execute("PRAGMA foreign_keys = ON")

        if connection.execute(
            "PRAGMA foreign_keys"
        ).fetchone()[0] != 1:
            raise MigrationError(
                "SQLite foreign-key enforcement is disabled."
            )

        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        migration_transaction_started = True

        _ensure_migration_table(connection)

        rows = connection.execute(
            """
            SELECT version, name
            FROM schema_migrations
            ORDER BY version
            """
        ).fetchall()

        applied_versions = {row[0] for row in rows}

        # Reject databases created by a newer application version.
        unsupported = {
            version
            for version in applied_versions
            if version > LATEST_SCHEMA_VERSION
        }

        if unsupported:
            raise MigrationError(
                "Database schema is newer than this application supports: "
                + ", ".join(
                    str(version)
                    for version in sorted(unsupported)
                )
            )

        applied = []

        # Migration history must be sequential.
        if 2 in applied_versions and 1 not in applied_versions:
            raise MigrationError(
                "Invalid migration history: Version 2 exists without "
                "Version 1. No automatic repair was attempted."
            )

        if 3 in applied_versions and not {1, 2}.issubset(applied_versions):
            raise MigrationError(
                "Invalid migration history: Version 3 exists without "
                "Versions 1 and 2. No automatic repair was attempted."
            )

        if 4 in applied_versions and not {1, 2, 3}.issubset(applied_versions):
            raise MigrationError(
                "Invalid migration history: Version 4 exists without "
                "Versions 1, 2 and 3. No automatic repair was attempted."
            )
        if 5 in applied_versions and not {1, 2, 3, 4}.issubset(applied_versions):
            raise MigrationError(
                "Invalid migration history: Version 5 exists without "
                "Versions 1, 2, 3 and 4. No automatic repair was attempted."
            )

        if 6 in applied_versions and not {1, 2, 3, 4, 5}.issubset(applied_versions):
            raise MigrationError(
                "Invalid migration history: Version 6 exists without "
                "Versions 1, 2, 3, 4 and 5. No automatic repair was attempted."
            )

        if 7 in applied_versions and not {1, 2, 3, 4, 5, 6}.issubset(applied_versions):
            raise MigrationError(
                "Invalid migration history: Version 7 exists without "
                "Versions 1, 2, 3, 4, 5 and 6. No automatic repair was attempted."
            )

        if 10 in applied_versions and not set(range(1, 10)).issubset(applied_versions):
            raise MigrationError(
                "Invalid migration history: Version 10 exists without Versions 1 through 9. "
                "No automatic repair was attempted."
            )

        if 11 in applied_versions and not set(range(1, 11)).issubset(applied_versions):
            raise MigrationError(
                "Invalid migration history: Version 11 exists without Versions 1 through 10. "
                "No automatic repair was attempted."
            )

        # ---------------------------------------------------------
        # VERSION 1: CONVERSATION PERSISTENCE
        # ---------------------------------------------------------

        if 1 not in applied_versions:

            existing = _conversation_tables_present(connection)

            if existing and existing != CONVERSATION_TABLES:
                missing = CONVERSATION_TABLES - existing

                raise MigrationError(
                    "Found a partial, unversioned conversation schema. "
                    "No automatic repair was attempted. Missing tables: "
                    + ", ".join(sorted(missing))
                )

            _apply_conversation_persistence_schema(connection)

            applied_at = datetime.now(timezone.utc).isoformat()

            connection.execute(
                """
                INSERT INTO schema_migrations
                    (version, name, applied_at)
                VALUES (?, ?, ?)
                """,
                (
                    1,
                    "conversation_persistence",
                    applied_at,
                ),
            )

            applied.append(1)

        else:
            # A recorded Version 1 must have a valid conversation schema.
            _validate_conversation_schema(connection)

        # ---------------------------------------------------------
        # VERSION 2: WEBHOOK EVENT PERSISTENCE
        # ---------------------------------------------------------

        if 2 not in applied_versions:

            _apply_webhook_event_persistence_schema(connection)

            applied_at = datetime.now(timezone.utc).isoformat()

            connection.execute(
                """
                INSERT INTO schema_migrations
                    (version, name, applied_at)
                VALUES (?, ?, ?)
                """,
                (
                    2,
                    "webhook_event_persistence",
                    applied_at,
                ),
            )

            applied.append(2)

        # ---------------------------------------------------------
        # VERSION 3: WEBHOOK EVENT ORDERING
        # ---------------------------------------------------------

        if 3 not in applied_versions:

            _apply_webhook_event_ordering_schema(connection)

            applied_at = datetime.now(timezone.utc).isoformat()

            connection.execute(
                """
                INSERT INTO schema_migrations
                    (version, name, applied_at)
                VALUES (?, ?, ?)
                """,
                (
                    3,
                    "webhook_event_ordering",
                    applied_at,
                ),
            )

            applied.append(3)


        # ---------------------------------------------------------
        # VERSION 4: CHANNEL-INDEPENDENT NOTIFICATION PREFERENCES
        # ---------------------------------------------------------

        if 4 not in applied_versions:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS notification_channel_preferences (
                    preference_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    channel TEXT NOT NULL CHECK (
                        channel IN ('telegram', 'email', 'sms', 'whatsapp')
                    ),
                    recipient TEXT,
                    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
                    verified_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (user_id, channel),
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                )
            """)

            connection.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_notification_channel_preferences_user_enabled
                ON notification_channel_preferences(user_id, enabled, channel)
            """)

            # Backfill existing preferences without asserting that legacy
            # addresses have been independently verified. Such rows remain
            # ineligible for delivery until verified_at is populated.
            if _table_exists(connection, "notification_preferences"):
                legacy_columns = {
                    row[1] for row in connection.execute(
                        'PRAGMA table_info("notification_preferences")'
                    ).fetchall()
                }
                required_legacy = {
                    "user_id", "email", "mobile_number", "email_enabled",
                    "sms_enabled", "whatsapp_enabled", "created_at", "updated_at",
                }
                if required_legacy.issubset(legacy_columns):
                    connection.execute("""
                        INSERT INTO notification_channel_preferences (
                            user_id, channel, recipient, enabled,
                            verified_at, created_at, updated_at
                        )
                        SELECT user_id, 'email', email, email_enabled,
                               NULL, created_at, updated_at
                        FROM notification_preferences
                        WHERE email IS NOT NULL AND TRIM(email) <> ''
                        ON CONFLICT(user_id, channel) DO NOTHING
                    """)
                    connection.execute("""
                        INSERT INTO notification_channel_preferences (
                            user_id, channel, recipient, enabled,
                            verified_at, created_at, updated_at
                        )
                        SELECT user_id, 'sms', mobile_number, sms_enabled,
                               NULL, created_at, updated_at
                        FROM notification_preferences
                        WHERE mobile_number IS NOT NULL AND TRIM(mobile_number) <> ''
                        ON CONFLICT(user_id, channel) DO NOTHING
                    """)
                    connection.execute("""
                        INSERT INTO notification_channel_preferences (
                            user_id, channel, recipient, enabled,
                            verified_at, created_at, updated_at
                        )
                        SELECT user_id, 'whatsapp', mobile_number, whatsapp_enabled,
                               NULL, created_at, updated_at
                        FROM notification_preferences
                        WHERE mobile_number IS NOT NULL AND TRIM(mobile_number) <> ''
                        ON CONFLICT(user_id, channel) DO NOTHING
                    """)

            applied_at = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """
                INSERT INTO schema_migrations (version, name, applied_at)
                VALUES (?, ?, ?)
                """,
                (4, "notification_channel_preferences", applied_at),
            )
            applied.append(4)

        # ---------------------------------------------------------
        # VERSION 5: DURABLE NOTIFICATION DELIVERY
        # ---------------------------------------------------------

        # Version 5 belongs to the notification-delivery subsystem. Some
        # legitimate migration callers (including preference-only setup and
        # tests) have not created the notifications table yet. Defer Version 5
        # in that case; do not record it as applied. A later migration run will
        # apply it once the notifications table exists.
        notifications_table_exists = _table_exists(connection, "notifications")
        if 5 not in applied_versions and notifications_table_exists:
            notification_columns = {
                row[1] for row in connection.execute(
                    'PRAGMA table_info("notifications")'
                ).fetchall()
            }
            additions = {
                "claimed_by": "TEXT",
                "claimed_at": "TEXT",
                "lease_expires_at": "TEXT",
                "next_retry_at": "TEXT",
            }
            for column_name, column_type in additions.items():
                if column_name not in notification_columns:
                    connection.execute(
                        f'ALTER TABLE notifications ADD COLUMN "{column_name}" {column_type}'
                    )
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_notifications_delivery_due
                ON notifications(delivery_status, next_retry_at, lease_expires_at, created_at)
            """)
            applied_at = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """INSERT INTO schema_migrations (version, name, applied_at)
                   VALUES (?, ?, ?)""",
                (5, "durable_notification_delivery", applied_at),
            )
            applied.append(5)

        # If Version 5 is recorded, its table and columns must still exist.
        # If the table has not yet been created, Version 5 remains pending.
        if 5 in applied_versions or 5 in applied:
            if not notifications_table_exists:
                raise MigrationError(
                    "Notification delivery schema validation failed: "
                    "Version 5 is recorded but notifications table is missing."
                )
            required_notification_delivery_columns = {
                "notification_id", "delivery_status", "attempt_count",
                "claimed_by", "claimed_at", "lease_expires_at", "next_retry_at",
            }
            actual_notification_columns = {
                row[1] for row in connection.execute(
                    'PRAGMA table_info("notifications")'
                ).fetchall()
            }
            missing_notification_columns = (
                required_notification_delivery_columns - actual_notification_columns
            )
            if missing_notification_columns:
                raise MigrationError(
                    "Notification delivery schema is incomplete. Missing columns: "
                    + ", ".join(sorted(missing_notification_columns))
                )

        # ---------------------------------------------------------
        # VERSION 6: PROACTIVE SHIPMENT MONITORING JOBS
        # ---------------------------------------------------------

        # Monitoring jobs coordinate one provider check per shipment, even
        # when multiple users subscribe to that shipment. Defer this migration
        # until the existing shipment/subscription tables are present and
        # Version 5 has been applied. This preserves the established staged
        # migration behaviour for partial database setup and tests.
        monitoring_prerequisites_exist = (
            _table_exists(connection, "shipments")
            and _table_exists(connection, "user_shipments")
        )
        if (
            6 not in applied_versions
            and (5 in applied_versions or 5 in applied)
            and monitoring_prerequisites_exist
        ):
            connection.execute("""
                CREATE TABLE IF NOT EXISTS monitoring_jobs (
                    monitoring_job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    shipment_id INTEGER NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'scheduled'
                        CHECK (status IN (
                            'scheduled', 'processing', 'retryable',
                            'completed', 'disabled'
                        )),
                    next_run_at TEXT,
                    claimed_by TEXT,
                    claimed_at TEXT,
                    lease_expires_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0
                        CHECK (attempt_count >= 0),
                    last_attempt_at TEXT,
                    last_success_at TEXT,
                    next_retry_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (shipment_id)
                        REFERENCES shipments(shipment_id) ON DELETE CASCADE
                )
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_monitoring_jobs_due
                ON monitoring_jobs(status, next_run_at)
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_monitoring_jobs_lease
                ON monitoring_jobs(status, lease_expires_at)
            """)

            # Backfill exactly one job for each shipment with at least one
            # active monitoring subscription. Do not use SQL MIN() on ISO
            # timestamp text: timestamps with different UTC offsets are not
            # necessarily ordered lexicographically. Parse and compare actual
            # instants, then store the selected run time in canonical UTC.
            # A null next_check_at means the subscription is eligible now.
            migration_now_dt = datetime.now(timezone.utc)
            migration_now = migration_now_dt.isoformat(timespec="seconds")
            active_subscriptions = connection.execute("""
                SELECT
                    s.shipment_id,
                    s.current_status,
                    us.next_check_at
                FROM shipments AS s
                INNER JOIN user_shipments AS us
                    ON us.shipment_id = s.shipment_id
                WHERE us.monitoring_enabled = 1
                  AND us.monitoring_completed_at IS NULL
                ORDER BY s.shipment_id
            """).fetchall()

            grouped_subscriptions = {}
            for shipment_id, current_status, next_check_at in active_subscriptions:
                grouped_subscriptions.setdefault(
                    shipment_id, {"status": current_status, "times": []}
                )
                if next_check_at is None:
                    due_at = migration_now_dt
                else:
                    try:
                        due_at = datetime.fromisoformat(next_check_at)
                    except (TypeError, ValueError) as exc:
                        raise MigrationError(
                            "Migration 6 encountered an invalid next_check_at "
                            f"timestamp for shipment {shipment_id}: {next_check_at!r}"
                        ) from exc
                    if due_at.tzinfo is None:
                        # Legacy naive timestamps are interpreted as UTC to
                        # avoid machine-local timezone-dependent migrations.
                        due_at = due_at.replace(tzinfo=timezone.utc)
                    due_at = due_at.astimezone(timezone.utc)
                grouped_subscriptions[shipment_id]["times"].append(due_at)

            for shipment_id, details in grouped_subscriptions.items():
                delivered = (
                    str(details["status"] or "").strip().lower() == "delivered"
                )
                next_run_at = (
                    None if delivered else min(details["times"]).isoformat(timespec="seconds")
                )
                job_status = "completed" if delivered else "scheduled"
                connection.execute("""
                    INSERT INTO monitoring_jobs (
                        shipment_id, status, next_run_at,
                        attempt_count, created_at, updated_at
                    ) VALUES (?, ?, ?, 0, ?, ?)
                    ON CONFLICT(shipment_id) DO NOTHING
                """, (shipment_id, job_status, next_run_at, migration_now, migration_now))

            applied_at = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """INSERT INTO schema_migrations (version, name, applied_at)
                   VALUES (?, ?, ?)""",
                (6, "proactive_shipment_monitoring", applied_at),
            )
            applied.append(6)

        if 6 in applied_versions or 6 in applied:
            if not _table_exists(connection, "monitoring_jobs"):
                raise MigrationError(
                    "Monitoring schema validation failed: monitoring_jobs table is missing."
                )
            required_monitoring_columns = {
                "monitoring_job_id", "shipment_id", "status", "next_run_at",
                "claimed_by", "claimed_at", "lease_expires_at", "attempt_count",
                "last_attempt_at", "last_success_at", "next_retry_at",
                "last_error", "created_at", "updated_at",
            }
            actual_monitoring_columns = {
                row[1] for row in connection.execute(
                    'PRAGMA table_info("monitoring_jobs")'
                ).fetchall()
            }
            missing_monitoring_columns = required_monitoring_columns - actual_monitoring_columns
            if missing_monitoring_columns:
                raise MigrationError(
                    "Monitoring schema is incomplete. Missing columns: "
                    + ", ".join(sorted(missing_monitoring_columns))
                )
            monitoring_foreign_keys = connection.execute(
                'PRAGMA foreign_key_list("monitoring_jobs")'
            ).fetchall()
            if not any(
                row[2] == "shipments" and row[3] == "shipment_id"
                and row[4] == "shipment_id"
                for row in monitoring_foreign_keys
            ):
                raise MigrationError(
                    "Monitoring schema is missing its shipment foreign key."
                )

        # ---------------------------------------------------------
        # VERSION 7: DURABLE SHIPMENT CHANGE EVENTS
        # ---------------------------------------------------------

        # Event history is installed only after the monitoring schema has
        # been installed. This preserves the existing deferred migration
        # behaviour for partially-created databases.
        if 7 not in applied_versions and (6 in applied_versions or 6 in applied):
            connection.execute("""
                CREATE TABLE IF NOT EXISTS shipment_change_events (
                    change_event_id TEXT PRIMARY KEY,
                    shipment_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL CHECK (
                        event_type IN ('shipment.status_changed', 'shipment.checkpoint_added')
                    ),
                    schema_version INTEGER NOT NULL CHECK (schema_version = 2),
                    source TEXT NOT NULL CHECK (source IN ('webhook', 'monitoring')),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    event_payload TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (shipment_id)
                        REFERENCES shipments(shipment_id) ON DELETE CASCADE
                )
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_shipment_change_events_shipment_time
                ON shipment_change_events(shipment_id, recorded_at, change_event_id)
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_shipment_change_events_type_time
                ON shipment_change_events(event_type, recorded_at)
            """)
            applied_at = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """INSERT INTO schema_migrations (version, name, applied_at)
                   VALUES (?, ?, ?)""",
                (7, "durable_shipment_change_events", applied_at),
            )
            applied.append(7)

        if 7 in applied_versions or 7 in applied:
            if not _table_exists(connection, "shipment_change_events"):
                raise MigrationError(
                    "Shipment change-event schema validation failed: "
                    "shipment_change_events table is missing."
                )
            required_change_event_columns = {
                "change_event_id", "shipment_id", "event_type", "schema_version",
                "source", "idempotency_key", "event_payload", "occurred_at",
                "recorded_at", "created_at",
            }
            actual_change_event_columns = {
                row[1] for row in connection.execute(
                    'PRAGMA table_info("shipment_change_events")'
                ).fetchall()
            }
            missing_change_event_columns = (
                required_change_event_columns - actual_change_event_columns
            )
            if missing_change_event_columns:
                raise MigrationError(
                    "Shipment change-event schema is incomplete. Missing columns: "
                    + ", ".join(sorted(missing_change_event_columns))
                )
            change_event_foreign_keys = connection.execute(
                'PRAGMA foreign_key_list("shipment_change_events")'
            ).fetchall()
            if not any(
                row[2] == "shipments" and row[3] == "shipment_id"
                and row[4] == "shipment_id"
                for row in change_event_foreign_keys
            ):
                raise MigrationError(
                    "Shipment change-event schema is missing its shipment foreign key."
                )

        # ---------------------------------------------------------
        # VERSION 8: INDIVIDUAL NOTIFICATION DELIVERY ATTEMPT HISTORY
        # ---------------------------------------------------------

        # Defer until the durable notification-delivery schema exists. This
        # keeps partial/preference-only databases on the established migration
        # path and makes the history additive to existing notification rows.
        if (
            8 not in applied_versions
            and (5 in applied_versions or 5 in applied)
            and _table_exists(connection, "notifications")
        ):
            connection.execute("""
                CREATE TABLE IF NOT EXISTS notification_delivery_attempts (
                    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    notification_id INTEGER NOT NULL,
                    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
                    worker_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    outcome TEXT NOT NULL CHECK (
                        outcome IN ('in_progress', 'sent', 'retry_scheduled',
                                    'failed', 'interrupted')
                    ),
                    provider_message_id TEXT,
                    error_message TEXT,
                    UNIQUE (notification_id, attempt_number),
                    FOREIGN KEY (notification_id)
                        REFERENCES notifications(notification_id) ON DELETE CASCADE
                )
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_notification_delivery_attempts_notification
                ON notification_delivery_attempts(notification_id, attempt_number)
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_notification_delivery_attempts_outcome_time
                ON notification_delivery_attempts(outcome, started_at)
            """)
            applied_at = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """INSERT INTO schema_migrations (version, name, applied_at)
                   VALUES (?, ?, ?)""",
                (8, "notification_delivery_attempt_history", applied_at),
            )
            applied.append(8)

        if 8 in applied_versions or 8 in applied:
            if not _table_exists(connection, "notification_delivery_attempts"):
                raise MigrationError(
                    "Notification delivery-attempt schema validation failed: table is missing."
                )
            required_attempt_columns = {
                "attempt_id", "notification_id", "attempt_number", "worker_id",
                "channel", "provider", "started_at", "completed_at", "outcome",
                "provider_message_id", "error_message",
            }
            actual_attempt_columns = {
                row[1] for row in connection.execute(
                    'PRAGMA table_info("notification_delivery_attempts")'
                ).fetchall()
            }
            missing_attempt_columns = required_attempt_columns - actual_attempt_columns
            if missing_attempt_columns:
                raise MigrationError(
                    "Notification delivery-attempt schema is incomplete. Missing columns: "
                    + ", ".join(sorted(missing_attempt_columns))
                )
            attempt_foreign_keys = connection.execute(
                'PRAGMA foreign_key_list("notification_delivery_attempts")'
            ).fetchall()
            if not any(
                row[2] == "notifications" and row[3] == "notification_id"
                and row[4] == "notification_id" for row in attempt_foreign_keys
            ):
                raise MigrationError(
                    "Notification delivery-attempt schema is missing its notification foreign key."
                )

        # ---------------------------------------------------------
        # VERSION 9: PROVIDER DELIVERY RECEIPTS
        # ---------------------------------------------------------

        # Receipt persistence is additive and depends on the notification
        # and attempt-history tables. Do not fabricate receipts for providers
        # or events that have not actually supplied a receipt.
        if 9 not in applied_versions and (8 in applied_versions or 8 in applied):
            connection.execute("""
                CREATE TABLE IF NOT EXISTS notification_delivery_receipts (
                    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    notification_id INTEGER NOT NULL,
                    attempt_id INTEGER,
                    provider TEXT NOT NULL,
                    provider_event_id TEXT NOT NULL,
                    provider_message_id TEXT NOT NULL,
                    receipt_status TEXT NOT NULL CHECK (
                        receipt_status IN ('submitted', 'delivered', 'failed', 'read')
                    ),
                    provider_event_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    processed_at TEXT NOT NULL,
                    FOREIGN KEY (notification_id)
                        REFERENCES notifications(notification_id) ON DELETE CASCADE,
                    FOREIGN KEY (attempt_id)
                        REFERENCES notification_delivery_attempts(attempt_id) ON DELETE SET NULL,
                    UNIQUE (provider, provider_event_id)
                )
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_notification_receipts_notification_time
                ON notification_delivery_receipts(notification_id, provider_event_at, receipt_id)
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_notification_receipts_provider_message
                ON notification_delivery_receipts(provider, provider_message_id)
            """)
            applied_at = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """INSERT INTO schema_migrations (version, name, applied_at)
                   VALUES (?, ?, ?)""",
                (9, "notification_delivery_receipts", applied_at),
            )
            applied.append(9)

        if 9 in applied_versions or 9 in applied:
            if not _table_exists(connection, "notification_delivery_receipts"):
                raise MigrationError(
                    "Notification receipt schema validation failed: table is missing."
                )
            required_receipt_columns = {
                "receipt_id", "notification_id", "attempt_id", "provider",
                "provider_event_id", "provider_message_id", "receipt_status",
                "provider_event_at", "received_at", "processed_at",
            }
            actual_receipt_columns = {
                row[1] for row in connection.execute(
                    'PRAGMA table_info("notification_delivery_receipts")'
                ).fetchall()
            }
            missing_receipt_columns = required_receipt_columns - actual_receipt_columns
            if missing_receipt_columns:
                raise MigrationError(
                    "Notification receipt schema is incomplete. Missing columns: "
                    + ", ".join(sorted(missing_receipt_columns))
                )
            receipt_foreign_keys = connection.execute(
                'PRAGMA foreign_key_list("notification_delivery_receipts")'
            ).fetchall()
            required_receipt_fks = {
                ("notifications", "notification_id", "notification_id"),
                ("notification_delivery_attempts", "attempt_id", "attempt_id"),
            }
            actual_receipt_fks = {
                (row[2], row[3], row[4]) for row in receipt_foreign_keys
            }
            if not required_receipt_fks.issubset(actual_receipt_fks):
                raise MigrationError(
                    "Notification receipt schema is missing required foreign keys."
                )

        # ---------------------------------------------------------
        # VERSION 10: PERSISTENT LOCAL BROWSER SESSIONS
        # ---------------------------------------------------------

        if 10 not in applied_versions and (9 in applied_versions or 9 in applied):
            connection.execute("""
                CREATE TABLE local_web_sessions (
                    token_hash TEXT PRIMARY KEY CHECK (length(token_hash) = 64),
                    active_conversation_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY (active_conversation_id)
                        REFERENCES conversations(conversation_id) ON DELETE SET NULL
                )
            """)
            connection.execute("""
                CREATE TABLE local_web_session_conversations (
                    token_hash TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (token_hash, conversation_id),
                    UNIQUE (conversation_id),
                    FOREIGN KEY (token_hash)
                        REFERENCES local_web_sessions(token_hash) ON DELETE CASCADE,
                    FOREIGN KEY (conversation_id)
                        REFERENCES conversations(conversation_id) ON DELETE CASCADE
                )
            """)
            connection.execute("""
                CREATE INDEX idx_local_web_session_conversations_conversation
                ON local_web_session_conversations(conversation_id)
            """)
            applied_at = datetime.now(timezone.utc).isoformat()
            connection.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (10, "persistent_local_web_sessions", applied_at),
            )
            applied.append(10)

        if 10 in applied_versions or 10 in applied:
            required_local_web_tables = {
                "local_web_sessions", "local_web_session_conversations"
            }
            missing_local_web_tables = {
                name for name in required_local_web_tables
                if not _table_exists(connection, name)
            }
            if missing_local_web_tables:
                raise MigrationError(
                    "Local browser-session schema is incomplete. Missing tables: "
                    + ", ".join(sorted(missing_local_web_tables))
                )
            required_session_columns = {
                "token_hash", "active_conversation_id", "created_at", "updated_at", "expires_at"
            }
            actual_session_columns = {
                row[1] for row in connection.execute(
                    'PRAGMA table_info("local_web_sessions")'
                ).fetchall()
            }
            if not required_session_columns.issubset(actual_session_columns):
                raise MigrationError("Local browser-session table is missing required columns.")
            required_association_columns = {"token_hash", "conversation_id", "created_at"}
            actual_association_columns = {
                row[1] for row in connection.execute(
                    'PRAGMA table_info("local_web_session_conversations")'
                ).fetchall()
            }
            if not required_association_columns.issubset(actual_association_columns):
                raise MigrationError("Local browser-session association table is missing required columns.")

        # ---------------------------------------------------------
        # VERSION 11: AUTHENTICATION, GUEST IDENTITY & NOTIFICATION OWNERSHIP
        # ---------------------------------------------------------

        if 11 not in applied_versions and (10 in applied_versions or 10 in applied):
            # 1. Registered-user passwordless auth tables
            schema.create_user_auth_identities_table(connection=connection)
            schema.create_auth_challenges_table(connection=connection)
            schema.create_auth_sessions_table(connection=connection)

            # 2. Guest notification identity & session tables
            schema.create_guest_notification_identities_table(connection=connection)
            schema.create_guest_notification_sessions_table(connection=connection)
            schema.create_guest_shipment_subscriptions_table(connection=connection)
            schema.create_guest_notification_channel_preferences_table(connection=connection)

            # 3. Telegram linking challenges
            schema.create_telegram_link_challenges_table(connection=connection)

            # 4. Rebuild notifications table to support guest_subscription_id and mutual exclusivity
            if _table_exists(connection, "notifications"):
                existing_cols = {
                    row[1] for row in connection.execute('PRAGMA table_info("notifications")').fetchall()
                }
                if "guest_subscription_id" not in existing_cols:
                    checkpoint_fk = (
                        ", FOREIGN KEY (checkpoint_id) REFERENCES checkpoints(checkpoint_id)"
                        if _table_exists(connection, "checkpoints")
                        else ""
                    )
                    connection.execute(f"""
                        CREATE TABLE notifications_v11 (
                            notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_shipment_id INTEGER,
                            guest_subscription_id INTEGER,
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
                            claimed_by TEXT,
                            claimed_at TEXT,
                            lease_expires_at TEXT,
                            next_retry_at TEXT,
                            CHECK (
                                (user_shipment_id IS NOT NULL AND guest_subscription_id IS NULL) OR
                                (user_shipment_id IS NULL AND guest_subscription_id IS NOT NULL)
                            ),
                            FOREIGN KEY (user_shipment_id) REFERENCES user_shipments(user_shipment_id) ON DELETE CASCADE,
                            FOREIGN KEY (guest_subscription_id) REFERENCES guest_shipment_subscriptions(guest_subscription_id) ON DELETE CASCADE
                            {checkpoint_fk}
                        )
                    """)
                    connection.execute("""
                        INSERT INTO notifications_v11 (
                            notification_id, user_shipment_id, guest_subscription_id,
                            checkpoint_id, notification_type, recipient, previous_status,
                            new_status, notification_message, delivery_status, provider_message_id,
                            idempotency_key, attempt_count, created_at, sent_at, last_attempt_at,
                            error_message, claimed_by, claimed_at, lease_expires_at, next_retry_at
                        )
                        SELECT
                            notification_id, user_shipment_id, NULL,
                            checkpoint_id, notification_type, recipient, previous_status,
                            new_status, notification_message, delivery_status, provider_message_id,
                            idempotency_key, attempt_count, created_at, sent_at, last_attempt_at,
                            error_message, claimed_by, claimed_at, lease_expires_at, next_retry_at
                        FROM notifications
                    """)
                    connection.execute("DROP TABLE notifications")
                    connection.execute("ALTER TABLE notifications_v11 RENAME TO notifications")
                    connection.execute("""
                        CREATE INDEX IF NOT EXISTS idx_notifications_delivery_due
                        ON notifications(delivery_status, next_retry_at, lease_expires_at, created_at)
                    """)
                    connection.execute("""
                        CREATE INDEX IF NOT EXISTS idx_notifications_guest_sub
                        ON notifications(guest_subscription_id)
                    """)
                    connection.execute("""
                        CREATE INDEX IF NOT EXISTS idx_notifications_user_sub
                        ON notifications(user_shipment_id)
                    """)

            applied_at = datetime.now(timezone.utc).isoformat()
            connection.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (11, "auth_guest_and_notification_ownership", applied_at),
            )
            applied.append(11)

        if 11 in applied_versions or 11 in applied:
            required_v11_tables = {
                "user_auth_identities",
                "auth_challenges",
                "auth_sessions",
                "guest_notification_identities",
                "guest_notification_sessions",
                "guest_shipment_subscriptions",
                "guest_notification_channel_preferences",
                "telegram_link_challenges",
            }
            missing_v11 = {name for name in required_v11_tables if not _table_exists(connection, name)}
            if missing_v11:
                raise MigrationError(
                    "Milestone 3 auth and guest schema is incomplete. Missing tables: "
                    + ", ".join(sorted(missing_v11))
                )
            if _table_exists(connection, "notifications"):
                notif_cols = {
                    row[1] for row in connection.execute('PRAGMA table_info("notifications")').fetchall()
                }
                if "guest_subscription_id" not in notif_cols:
                    raise MigrationError(
                        "Notifications table is missing guest_subscription_id after Version 11 migration."
                    )


        # ---------------------------------------------------------
        # FINAL SCHEMA VALIDATION
        # ---------------------------------------------------------

        _validate_conversation_schema(connection)

        required_webhook_columns = {
            "event_record_id",
            "provider",
            "provider_event_id",
            "event_type",
            "provider_event_timestamp",
            "provider_tracking_id",
            "tracking_number",
            "carrier",
            "raw_payload",
            "received_at",
            "status",
            "processing_attempts",
            "next_retry_at",
            "claimed_by",
            "claimed_at",
            "lease_expires_at",
            "processed_at",
            "last_error",
        }

        if not _table_exists(connection, "webhook_events"):
            raise MigrationError(
                "Webhook event schema validation failed: "
                "webhook_events table is missing."
            )

        actual_webhook_columns = {
            row[1]
            for row in connection.execute(
                'PRAGMA table_info("webhook_events")'
            ).fetchall()
        }

        missing_webhook_columns = (
            required_webhook_columns - actual_webhook_columns
        )

        if missing_webhook_columns:
            raise MigrationError(
                "Webhook event schema is incomplete. Missing columns: "
                + ", ".join(sorted(missing_webhook_columns))
            )

        # Validate the shipment event-ordering watermark.
        if not _table_exists(connection, "shipments"):
            raise MigrationError(
                "Shipment schema validation failed: "
                "shipments table is missing."
            )

        shipment_columns = {
            row[1]
            for row in connection.execute(
                'PRAGMA table_info("shipments")'
            ).fetchall()
        }

        if "last_provider_event_timestamp" not in shipment_columns:
            raise MigrationError(
                "Shipment schema is incomplete: "
                "last_provider_event_timestamp is missing."
            )

        required_channel_preference_columns = {
            "preference_id", "user_id", "channel", "recipient",
            "enabled", "verified_at", "created_at", "updated_at",
        }
        if not _table_exists(connection, "notification_channel_preferences"):
            raise MigrationError(
                "Notification preference schema validation failed: "
                "notification_channel_preferences table is missing."
            )
        actual_channel_preference_columns = {
            row[1] for row in connection.execute(
                'PRAGMA table_info("notification_channel_preferences")'
            ).fetchall()
        }
        missing_channel_preference_columns = (
            required_channel_preference_columns - actual_channel_preference_columns
        )
        if missing_channel_preference_columns:
            raise MigrationError(
                "Notification preference schema is incomplete. Missing columns: "
                + ", ".join(sorted(missing_channel_preference_columns))
            )

        # Verify SQLite database integrity.
        integrity = connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]

        if integrity != "ok":
            raise MigrationError(
                "SQLite integrity check failed after migration: "
                f"{integrity}"
            )

        # Verify foreign-key relationships.
        foreign_key_errors = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

        if foreign_key_errors:
            raise MigrationError(
                "Foreign-key violations detected after migration: "
                f"{foreign_key_errors[:10]}"
            )

        connection.commit()
        migration_transaction_started = False
        connection.execute("PRAGMA foreign_keys = ON")

        current_version = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
        return {
            "current_version": current_version,
            "applied": applied,
            "already_current": len(applied) == 0,
        }

    except Exception:
        if migration_transaction_started and connection.in_transaction:
            connection.rollback()
        try:
            connection.execute("PRAGMA foreign_keys = ON")
        except Exception:
            pass

        raise

    finally:
        if owns_connection:
            connection.close()


def main():
    result = apply_migrations()
    if result["applied"]:
        print("Applied schema migration(s):", result["applied"])
    else:
        print("Database schema is already up to date.")
    print("Current schema version:", result["current_version"])


if __name__ == "__main__":
    main()
