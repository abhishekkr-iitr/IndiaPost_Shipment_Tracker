from database.db_connection import get_db_connection


def create_users_table(connection=None):

    owns_connection = connection is None
    if owns_connection:
        connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (

                user_id INTEGER PRIMARY KEY AUTOINCREMENT,

                external_user_id TEXT UNIQUE,

                created_at TEXT NOT NULL

            );
        """)

        if owns_connection:
            connection.commit()

        print("Users table created successfully!")

    finally:
        if owns_connection:
            connection.close()



def create_shipments_table(connection=None):

    owns_connection = connection is None
    if owns_connection:
        connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS shipments (

                shipment_id INTEGER PRIMARY KEY AUTOINCREMENT,

                tracking_number TEXT NOT NULL,

                carrier TEXT NOT NULL,

                provider TEXT NOT NULL,

                provider_tracking_id TEXT,

                current_status TEXT,

                status_message TEXT,

                origin TEXT,

                destination TEXT,

                pickup_date TEXT,

                delivery_date TEXT,

                last_checked_at TEXT,

                last_status_change_at TEXT,

                created_at TEXT NOT NULL,

                updated_at TEXT NOT NULL,

                UNIQUE (tracking_number, carrier)

            );
        """)

        if owns_connection:
            connection.commit()

        print("Shipments table created successfully!")

    finally:
        if owns_connection:
            connection.close()



def create_user_shipments_table(connection=None):

    owns_connection = connection is None
    if owns_connection:
        connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_shipments (

                user_shipment_id INTEGER PRIMARY KEY AUTOINCREMENT,

                user_id INTEGER NOT NULL,

                shipment_id INTEGER NOT NULL,

                monitoring_enabled INTEGER NOT NULL DEFAULT 1,

                monitoring_interval_hours INTEGER NOT NULL DEFAULT 6,

                next_check_at TEXT,

                monitoring_completed_at TEXT,

                created_at TEXT NOT NULL,

                UNIQUE (user_id, shipment_id),

                FOREIGN KEY (user_id)
                    REFERENCES users(user_id),

                FOREIGN KEY (shipment_id)
                    REFERENCES shipments(shipment_id)

            );
        """)

        if owns_connection:
            connection.commit()

        print("User Shipments table created successfully!")

    finally:
        if owns_connection:
            connection.close()



def create_checkpoints_table(connection=None):

    owns_connection = connection is None
    if owns_connection:
        connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS checkpoints (

                checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,

                shipment_id INTEGER NOT NULL,

                event_date TEXT,

                location TEXT,

                status TEXT,

                message TEXT,

                event_fingerprint TEXT NOT NULL,

                duration_since_previous_seconds INTEGER,

                created_at TEXT NOT NULL,

                UNIQUE (shipment_id, event_fingerprint),

                FOREIGN KEY (shipment_id)
                    REFERENCES shipments(shipment_id)

            );
        """)

        if owns_connection:
            connection.commit()

        print("Checkpoints table created successfully!")

    finally:
        if owns_connection:
            connection.close()



def create_tracking_checks_table(connection=None):

    owns_connection = connection is None
    if owns_connection:
        connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tracking_checks (

                check_id INTEGER PRIMARY KEY AUTOINCREMENT,

                shipment_id INTEGER NOT NULL,

                check_started_at TEXT NOT NULL,

                check_completed_at TEXT,

                check_status TEXT NOT NULL,

                retrieved_status TEXT,

                error_message TEXT,

                FOREIGN KEY (shipment_id)
                    REFERENCES shipments(shipment_id)

            );
        """)

        if owns_connection:
            connection.commit()

        print("Tracking Checks table created successfully!")

    finally:
        if owns_connection:
            connection.close()



def create_notification_preferences_table(connection=None):

    owns_connection = connection is None
    if owns_connection:
        connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notification_preferences (

                preference_id INTEGER PRIMARY KEY AUTOINCREMENT,

                user_id INTEGER NOT NULL UNIQUE,

                email TEXT,

                mobile_number TEXT,

                email_enabled INTEGER NOT NULL DEFAULT 1,

                sms_enabled INTEGER NOT NULL DEFAULT 0,

                whatsapp_enabled INTEGER NOT NULL DEFAULT 0,

                created_at TEXT NOT NULL,

                updated_at TEXT NOT NULL,

                FOREIGN KEY (user_id)
                    REFERENCES users(user_id)

            );
        """)

        if owns_connection:
            connection.commit()

        print("Notification Preferences table created successfully!")

    finally:
        if owns_connection:
            connection.close()



def create_notifications_table(connection=None):

    owns_connection = connection is None
    if owns_connection:
        connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notifications (

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

                FOREIGN KEY (user_shipment_id)
                    REFERENCES user_shipments(user_shipment_id),

                FOREIGN KEY (checkpoint_id)
                    REFERENCES checkpoints(checkpoint_id)

            );
        """)

        if owns_connection:
            connection.commit()

        print("Notifications table created successfully!")

    finally:
        if owns_connection:
            connection.close()




def create_conversations_table(connection=None):

    owns_connection = connection is None
    if owns_connection:
        connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversations (

                conversation_id TEXT PRIMARY KEY,

                user_id INTEGER,

                channel TEXT NOT NULL,

                channel_conversation_id TEXT,

                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'closed')),

                provider TEXT,

                provider_conversation_id TEXT,

                created_at TEXT NOT NULL,

                updated_at TEXT NOT NULL,

                closed_at TEXT,

                FOREIGN KEY (user_id)
                    REFERENCES users(user_id)
                    ON DELETE SET NULL

            );
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_conversations_user_updated
            ON conversations(user_id, updated_at);
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_conversations_channel_id
            ON conversations(channel, channel_conversation_id);
        """)

        if owns_connection:
            connection.commit()

        print("Conversations table created successfully!")

    finally:
        if owns_connection:
            connection.close()



def create_conversation_messages_table(connection=None):

    owns_connection = connection is None
    if owns_connection:
        connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversation_messages (

                message_id INTEGER PRIMARY KEY AUTOINCREMENT,

                conversation_id TEXT NOT NULL,

                sequence_number INTEGER NOT NULL,

                role TEXT NOT NULL
                    CHECK (
                        role IN (
                            'user',
                            'assistant',
                            'tool',
                            'system'
                        )
                    ),

                content TEXT NOT NULL,

                tool_name TEXT,

                tool_call_id TEXT,

                provider_message_id TEXT,

                created_at TEXT NOT NULL,

                UNIQUE (conversation_id, sequence_number),

                FOREIGN KEY (conversation_id)
                    REFERENCES conversations(conversation_id)
                    ON DELETE CASCADE

            );
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_conversation_messages_order
            ON conversation_messages(
                conversation_id,
                sequence_number
            );
        """)
        
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_conversation_messages_conversation_message
            ON conversation_messages(
                conversation_id,
                message_id
            );
        """)

        if owns_connection:
            connection.commit()

        print("Conversation Messages table created successfully!")

    finally:
        if owns_connection:
            connection.close()

def create_conversation_requests_table(connection=None):

    owns_connection = connection is None
    if owns_connection:
        connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversation_requests (

                request_id TEXT PRIMARY KEY,

                conversation_id TEXT NOT NULL,

                user_message_id INTEGER NOT NULL,

                response_message_id INTEGER,

                request_hash TEXT NOT NULL,

                status TEXT NOT NULL
                    CHECK (
                        status IN (
                            'processing',
                            'completed',
                            'failed',
                            'interrupted'
                        )
                    ),

                error_code TEXT,

                created_at TEXT NOT NULL,

                updated_at TEXT NOT NULL,

                completed_at TEXT,

                FOREIGN KEY (conversation_id)
                    REFERENCES conversations(conversation_id)
                    ON DELETE CASCADE,

                FOREIGN KEY (
                    conversation_id,
                    user_message_id
                )
                    REFERENCES conversation_messages(
                        conversation_id,
                        message_id
                    ),

                FOREIGN KEY (
                    conversation_id,
                    response_message_id
                )
                    REFERENCES conversation_messages(
                        conversation_id,
                        message_id
                    )

            );
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_conversation_requests_conversation_created
            ON conversation_requests(
                conversation_id,
                created_at
            );
        """)

        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_conversation_requests_conversation_request
            ON conversation_requests(
                conversation_id,
                request_id
            );
        """)

        if owns_connection:
            connection.commit()

        print("Conversation Requests table created successfully!")

    finally:
        if owns_connection:
            connection.close()


def create_conversation_tool_rounds_table(connection=None):
    """Create the table that tracks complete groups of tool calls."""

    owns_connection = connection is None
    if owns_connection:
        connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversation_tool_rounds (

                round_id TEXT PRIMARY KEY,

                conversation_id TEXT NOT NULL,

                request_id TEXT,

                round_number INTEGER NOT NULL
                    CHECK (round_number > 0),

                status TEXT NOT NULL
                    CHECK (
                        status IN (
                            'recording',
                            'pending',
                            'running',
                            'completed',
                            'failed',
                            'interrupted'
                        )
                    ),

                expected_call_count INTEGER NOT NULL
                    CHECK (expected_call_count > 0),

                created_at TEXT NOT NULL,

                updated_at TEXT NOT NULL,

                completed_at TEXT,

                FOREIGN KEY (conversation_id)
                    REFERENCES conversations(conversation_id)
                    ON DELETE CASCADE,

                FOREIGN KEY (
                    conversation_id,
                    request_id
                )
                    REFERENCES conversation_requests(
                        conversation_id,
                        request_id
                    )

            );
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_conversation_tool_rounds_request
            ON conversation_tool_rounds(
                request_id,
                round_number
            );
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_conversation_tool_rounds_status
            ON conversation_tool_rounds(
                conversation_id,
                status
            );
        """)

        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_conversation_tool_rounds_request_number
            ON conversation_tool_rounds(
                conversation_id,
                request_id,
                round_number
            )
            WHERE request_id IS NOT NULL;
        """)

        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_conversation_tool_rounds_no_request_number
            ON conversation_tool_rounds(
                conversation_id,
                round_number
            )
            WHERE request_id IS NULL;
        """)

        if owns_connection:
            connection.commit()

        print("Conversation Tool Rounds table created successfully!")

    finally:
        if owns_connection:
            connection.close()


def create_conversation_tool_calls_table(connection=None):

    owns_connection = connection is None
    if owns_connection:
        connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversation_tool_calls (

                tool_execution_id TEXT PRIMARY KEY,

                conversation_id TEXT NOT NULL,

                request_id TEXT,

                round_id TEXT,

                round_number INTEGER NOT NULL
                    CHECK (round_number > 0),

                tool_call_id TEXT NOT NULL,

                tool_name TEXT NOT NULL,

                arguments TEXT NOT NULL,

                call_message_id INTEGER NOT NULL,

                output_message_id INTEGER,

                status TEXT NOT NULL
                    CHECK (
                        status IN (
                            'pending',
                            'running',
                            'completed',
                            'failed',
                            'interrupted'
                        )
                    ),

                error_code TEXT,

                created_at TEXT NOT NULL,

                started_at TEXT,

                updated_at TEXT NOT NULL,

                completed_at TEXT,

                FOREIGN KEY (conversation_id)
                    REFERENCES conversations(conversation_id)
                    ON DELETE CASCADE,

                FOREIGN KEY (
                    conversation_id,
                    request_id
                )
                    REFERENCES conversation_requests(
                        conversation_id,
                        request_id
                    ),

                FOREIGN KEY (
                    conversation_id,
                    call_message_id
                )
                    REFERENCES conversation_messages(
                        conversation_id,
                        message_id
                    ),

                FOREIGN KEY (
                    conversation_id,
                    output_message_id
                )
                    REFERENCES conversation_messages(
                        conversation_id,
                        message_id
                    ),

                FOREIGN KEY (round_id)
                    REFERENCES conversation_tool_rounds(round_id),

                UNIQUE (
                    conversation_id,
                    tool_call_id
                )

            );
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_conversation_tool_calls_request_round
            ON conversation_tool_calls(
                request_id,
                round_number
            );
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_conversation_tool_calls_conversation_status
            ON conversation_tool_calls(
                conversation_id,
                status
            );
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_conversation_tool_calls_round
            ON conversation_tool_calls(round_id);
        """)

        if owns_connection:
            connection.commit()

        print("Conversation Tool Calls table created successfully!")

    finally:
        if owns_connection:
            connection.close()


def migrate_conversation_tool_calls_add_round_id(connection=None):
    """Add the nullable round_id column to an existing tool-call table."""

    owns_connection = connection is None
    if owns_connection:
        connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("PRAGMA table_info(conversation_tool_calls);")

        columns = {
            row[1]
            for row in cursor.fetchall()
        }

        if "round_id" not in columns:
            cursor.execute("""
                ALTER TABLE conversation_tool_calls
                ADD COLUMN round_id TEXT
                REFERENCES conversation_tool_rounds(round_id);
            """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_conversation_tool_calls_round
            ON conversation_tool_calls(round_id);
        """)

        if owns_connection:
            connection.commit()

        print("Conversation Tool Calls round_id migration completed!")

    except Exception:
        if owns_connection:
            connection.rollback()
        raise

    finally:
        if owns_connection:
            connection.close()


def create_user_auth_identities_table(connection=None):
    owns_connection = connection is None
    if owns_connection:
        connection = get_db_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_auth_identities (
                auth_identity_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                identity_type TEXT NOT NULL DEFAULT 'email' CHECK (identity_type IN ('email')),
                identifier TEXT NOT NULL,
                verified_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (identity_type, identifier),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_auth_identities_user_id
            ON user_auth_identities(user_id);
        """)
        if owns_connection:
            connection.commit()
        print("User Auth Identities table created successfully!")
    finally:
        if owns_connection:
            connection.close()


def create_auth_challenges_table(connection=None):
    owns_connection = connection is None
    if owns_connection:
        connection = get_db_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS auth_challenges (
                challenge_id TEXT PRIMARY KEY,
                identity_type TEXT NOT NULL DEFAULT 'email' CHECK (identity_type IN ('email')),
                identifier TEXT NOT NULL,
                challenge_hash TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
                expires_at TEXT NOT NULL,
                consumed_at TEXT,
                created_at TEXT NOT NULL
            );
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_auth_challenges_lookup
            ON auth_challenges(identifier, identity_type, expires_at, consumed_at);
        """)
        if owns_connection:
            connection.commit()
        print("Auth Challenges table created successfully!")
    finally:
        if owns_connection:
            connection.close()


def create_auth_sessions_table(connection=None):
    owns_connection = connection is None
    if owns_connection:
        connection = get_db_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash TEXT PRIMARY KEY CHECK (length(token_hash) = 64),
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                last_active_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
            ON auth_sessions(user_id, expires_at, revoked_at);
        """)
        if owns_connection:
            connection.commit()
        print("Auth Sessions table created successfully!")
    finally:
        if owns_connection:
            connection.close()


def create_guest_notification_identities_table(connection=None):
    owns_connection = connection is None
    if owns_connection:
        connection = get_db_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS guest_notification_identities (
                guest_identity_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                revoked_at TEXT,
                notes TEXT
            );
        """)
        if owns_connection:
            connection.commit()
        print("Guest Notification Identities table created successfully!")
    finally:
        if owns_connection:
            connection.close()


def create_guest_notification_sessions_table(connection=None):
    owns_connection = connection is None
    if owns_connection:
        connection = get_db_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS guest_notification_sessions (
                token_hash TEXT PRIMARY KEY CHECK (length(token_hash) = 64),
                guest_identity_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_active_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                FOREIGN KEY (guest_identity_id) REFERENCES guest_notification_identities(guest_identity_id) ON DELETE CASCADE
            );
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_guest_notification_sessions_guest
            ON guest_notification_sessions(guest_identity_id, expires_at, revoked_at);
        """)
        if owns_connection:
            connection.commit()
        print("Guest Notification Sessions table created successfully!")
    finally:
        if owns_connection:
            connection.close()


def create_guest_shipment_subscriptions_table(connection=None):
    owns_connection = connection is None
    if owns_connection:
        connection = get_db_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS guest_shipment_subscriptions (
                guest_subscription_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guest_identity_id TEXT NOT NULL,
                shipment_id INTEGER NOT NULL,
                monitoring_enabled INTEGER NOT NULL DEFAULT 1 CHECK (monitoring_enabled IN (0, 1)),
                monitoring_interval_hours INTEGER NOT NULL DEFAULT 6 CHECK (monitoring_interval_hours > 0),
                next_check_at TEXT,
                monitoring_completed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (guest_identity_id, shipment_id),
                FOREIGN KEY (guest_identity_id) REFERENCES guest_notification_identities(guest_identity_id) ON DELETE CASCADE,
                FOREIGN KEY (shipment_id) REFERENCES shipments(shipment_id) ON DELETE CASCADE
            );
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_guest_shipment_subscriptions_active
            ON guest_shipment_subscriptions(shipment_id, monitoring_enabled, monitoring_completed_at);
        """)
        if owns_connection:
            connection.commit()
        print("Guest Shipment Subscriptions table created successfully!")
    finally:
        if owns_connection:
            connection.close()


def create_guest_notification_channel_preferences_table(connection=None):
    owns_connection = connection is None
    if owns_connection:
        connection = get_db_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS guest_notification_channel_preferences (
                preference_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guest_identity_id TEXT NOT NULL,
                channel TEXT NOT NULL CHECK (channel IN ('telegram', 'email', 'sms', 'whatsapp')),
                recipient TEXT,
                enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
                verified_at TEXT,
                consent_at TEXT,
                consent_revoked_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (guest_identity_id, channel),
                FOREIGN KEY (guest_identity_id) REFERENCES guest_notification_identities(guest_identity_id) ON DELETE CASCADE
            );
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_guest_channel_preferences_active
            ON guest_notification_channel_preferences(guest_identity_id, channel, enabled, verified_at);
        """)
        if owns_connection:
            connection.commit()
        print("Guest Notification Channel Preferences table created successfully!")
    finally:
        if owns_connection:
            connection.close()


def create_telegram_link_challenges_table(connection=None):
    owns_connection = connection is None
    if owns_connection:
        connection = get_db_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS telegram_link_challenges (
                challenge_id TEXT PRIMARY KEY,
                owner_type TEXT NOT NULL CHECK (owner_type IN ('registered', 'guest')),
                user_id INTEGER,
                guest_identity_id TEXT,
                challenge_code_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_at TEXT,
                telegram_chat_id TEXT,
                created_at TEXT NOT NULL,
                CHECK (
                    (owner_type = 'registered' AND user_id IS NOT NULL AND guest_identity_id IS NULL) OR
                    (owner_type = 'guest' AND guest_identity_id IS NOT NULL AND user_id IS NULL)
                ),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (guest_identity_id) REFERENCES guest_notification_identities(guest_identity_id) ON DELETE CASCADE
            );
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_telegram_link_challenges_lookup
            ON telegram_link_challenges(challenge_code_hash, expires_at, consumed_at);
        """)
        if owns_connection:
            connection.commit()
        print("Telegram Link Challenges table created successfully!")
    finally:
        if owns_connection:
            connection.close()


