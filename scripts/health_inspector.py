"""System Health & Operational Diagnostics Inspector.

Performs non-destructive inspections of database health, schema versions,
worker queues, channel readiness, and environment configuration without
exposing sensitive credentials or customer data.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv

load_dotenv(dotenv_path=BASE_DIR / ".env", override=False)

from database.db_connection import get_db_connection


def _mask_secret(val: str | None, prefix_len: int = 4) -> str:
    if not val or not val.strip():
        return "[NOT CONFIGURED]"
    clean = val.strip()
    if len(clean) <= prefix_len + 3:
        return f"[CONFIGURED (len={len(clean)})]"
    return f"[CONFIGURED: {clean[:prefix_len]}... (len={len(clean)})]"


def inspect_environment() -> dict[str, Any]:
    """Inspect environment configuration and mask credentials."""
    env_file = BASE_DIR / ".env"
    return {
        "env_file_exists": env_file.exists(),
        "openai_api_key": _mask_secret(os.getenv("OPENAI_API_KEY"), 3),
        "aftership_api_key": _mask_secret(os.getenv("AFTERSHIP_API_KEY"), 4),
        "telegram_bot_token": _mask_secret(os.getenv("TELEGRAM_BOT_TOKEN"), 4),
        "telegram_webhook_secret": _mask_secret(os.getenv("TELEGRAM_WEBHOOK_SECRET"), 4),
        "log_level": os.getenv("LOG_LEVEL", "INFO").upper(),
    }


def inspect_database() -> dict[str, Any]:
    """Inspect database connectivity, schema version, integrity, and record counts."""
    conn = get_db_connection()
    try:
        db_path = conn.execute("PRAGMA database_list").fetchone()[2]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        
        # Determine latest migration
        has_migrations = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone() is not None
        
        latest_migration = 0
        if has_migrations:
            row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            latest_migration = row[0] if row and row[0] is not None else 0

        # Gather table counts safely
        def _count(table: str) -> int:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                return -1
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

        counts = {
            "shipments": _count("shipments"),
            "checkpoints": _count("checkpoints"),
            "users": _count("users"),
            "user_shipments": _count("user_shipments"),
            "guest_notification_identities": _count("guest_notification_identities"),
            "guest_shipment_subscriptions": _count("guest_shipment_subscriptions"),
            "notifications": _count("notifications"),
            "notification_delivery_receipts": _count("notification_delivery_receipts"),
            "monitoring_jobs": _count("monitoring_jobs"),
        }

        # Check pending queues
        pending_notifications = 0
        if counts["notifications"] >= 0:
            pending_notifications = conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE delivery_status IN ('pending', 'processing', 'retry')"
            ).fetchone()[0]

        active_monitoring_jobs = 0
        if counts["monitoring_jobs"] >= 0:
            active_monitoring_jobs = conn.execute(
                "SELECT COUNT(*) FROM monitoring_jobs WHERE status = 'scheduled'"
            ).fetchone()[0]

        return {
            "database_path": db_path,
            "sqlite_version": sqlite3.sqlite_version,
            "integrity": integrity,
            "foreign_key_violations": len(fk_errors),
            "schema_migration_version": latest_migration,
            "table_record_counts": counts,
            "queue_status": {
                "pending_notifications": pending_notifications,
                "active_monitoring_jobs": active_monitoring_jobs,
            },
        }
    finally:
        conn.close()


def inspect_channel_readiness() -> dict[str, Any]:
    """Inspect readiness of outbound notification adapters."""
    from notifications.delivery_setup import build_delivery_runtime

    try:
        runtime = build_delivery_runtime()
        telegram_ready = "telegram" in runtime.registry.channels()
    except Exception:
        telegram_ready = False

    return {
        "telegram_outbound_ready": telegram_ready,
        "telegram_inbound_secret_configured": bool(os.getenv("TELEGRAM_WEBHOOK_SECRET")),
    }


def run_full_diagnostics() -> dict[str, Any]:
    """Execute complete system health check."""
    env_info = inspect_environment()
    db_info = inspect_database()
    channel_info = inspect_channel_readiness()

    healthy = (
        db_info["integrity"] == "ok"
        and db_info["foreign_key_violations"] == 0
        and db_info["schema_migration_version"] >= 11
    )

    return {
        "healthy": healthy,
        "environment": env_info,
        "database": db_info,
        "channels": channel_info,
    }


def main() -> None:
    diagnostics = run_full_diagnostics()
    print("==================================================")
    print("  INDIA POST AI ASSISTANT - SYSTEM DIAGNOSTICS")
    print("==================================================")
    print(f"Overall Health Status: {'[HEALTHY]' if diagnostics['healthy'] else '[ATTENTION REQUIRED]'}")
    print("\n[Database]")
    print(f"  Path: {diagnostics['database']['database_path']}")
    print(f"  Schema Migration Version: {diagnostics['database']['schema_migration_version']}")
    print(f"  Integrity Check: {diagnostics['database']['integrity']}")
    print(f"  Foreign Key Violations: {diagnostics['database']['foreign_key_violations']}")
    print("  Key Table Counts:")
    for tbl, count in diagnostics['database']['table_record_counts'].items():
        if count >= 0:
            print(f"    - {tbl}: {count}")

    print("\n[Queues & Jobs]")
    print(f"  Active Monitoring Jobs: {diagnostics['database']['queue_status']['active_monitoring_jobs']}")
    print(f"  Pending Outbox Notifications: {diagnostics['database']['queue_status']['pending_notifications']}")

    print("\n[Channels & Integrations]")
    print(f"  Telegram Outbound Ready: {diagnostics['channels']['telegram_outbound_ready']}")
    print(f"  Telegram Webhook Secret Configured: {diagnostics['channels']['telegram_inbound_secret_configured']}")
    print(f"  OpenAI API Key: {diagnostics['environment']['openai_api_key']}")
    print(f"  AfterShip API Key: {diagnostics['environment']['aftership_api_key']}")
    print("==================================================")

    sys.exit(0 if diagnostics["healthy"] else 1)


if __name__ == "__main__":
    main()
