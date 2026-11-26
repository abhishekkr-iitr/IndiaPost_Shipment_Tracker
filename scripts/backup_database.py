"""Automated database backup, integrity verification, and retention manager.

Performs transactional online SQLite backups, verifies data integrity and
foreign key consistency on the target copy, and enforces retention policies.
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from database.db_connection import get_db_connection

logger = logging.getLogger("database.backup")


def _get_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def perform_backup(
    destination_dir: Path | None = None,
    keep_count: int = 10,
) -> dict[str, Any]:
    """Execute an online transactional backup of the active database."""
    source_conn = get_db_connection()
    try:
        source_path = Path(
            source_conn.execute("PRAGMA database_list").fetchone()[2]
        ).resolve()
        
        backup_root = destination_dir or (BASE_DIR / "database" / "backup")
        timestamp = _get_timestamp()
        target_dir = backup_root / f"shipments_backup_{timestamp}"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_db_path = target_dir / "shipments.db"

        # Online SQLite backup API prevents torn pages and concurrency locks
        dest_conn = sqlite3.connect(str(target_db_path))
        try:
            source_conn.backup(dest_conn)
            dest_conn.commit()
        finally:
            dest_conn.close()

        # Verify target backup integrity
        verify_conn = sqlite3.connect(str(target_db_path))
        try:
            verify_conn.execute("PRAGMA foreign_keys = ON")
            integrity = verify_conn.execute("PRAGMA integrity_check").fetchone()[0]
            fk_errors = verify_conn.execute("PRAGMA foreign_key_check").fetchall()
            schema_version = verify_conn.execute("PRAGMA user_version").fetchone()[0]
            table_count = verify_conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
        finally:
            verify_conn.close()

        if integrity != "ok":
            raise RuntimeError(f"Backup failed integrity check: {integrity}")

        if fk_errors:
            raise RuntimeError(f"Backup failed foreign key check: {len(fk_errors)} violation(s)")

        # Prune older backups according to retention policy
        pruned = prune_old_backups(backup_root, keep_count)

        return {
            "success": True,
            "source_db": str(source_path),
            "backup_db": str(target_db_path),
            "timestamp": timestamp,
            "schema_version": schema_version,
            "table_count": table_count,
            "integrity": integrity,
            "foreign_key_violations": len(fk_errors),
            "pruned_backups": pruned,
        }
    finally:
        source_conn.close()


def prune_old_backups(backup_root: Path, keep_count: int) -> list[str]:
    """Prune oldest backup directories exceeding keep_count."""
    if not backup_root.exists() or keep_count <= 0:
        return []

    dirs = [
        d for d in backup_root.iterdir()
        if d.is_dir() and d.name.startswith("shipments_backup_")
    ]
    dirs.sort(key=lambda d: d.name)

    pruned = []
    while len(dirs) > keep_count:
        oldest = dirs.pop(0)
        try:
            shutil.rmtree(oldest)
            pruned.append(oldest.name)
        except Exception as exc:
            logger.warning("Could not prune old backup %s: %s", oldest.name, exc)

    return pruned


def verify_restore_target(backup_path: Path) -> dict[str, Any]:
    """Verify that a backup database can be mounted and read safely."""
    if not backup_path.exists():
        raise FileNotFoundError(f"Backup database not found: {backup_path}")

    conn = sqlite3.connect(str(backup_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        return {
            "verified": integrity == "ok" and len(fk_errors) == 0,
            "integrity": integrity,
            "foreign_key_errors": len(fk_errors),
            "schema_version": schema_version,
            "tables": tables,
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="India Post Assistant Database Backup Utility")
    parser.add_argument("--keep", type=int, default=10, help="Number of backups to retain")
    parser.add_argument("--verify", type=str, default=None, help="Verify a specific backup file")
    args = parser.parse_args()

    if args.verify:
        result = verify_restore_target(Path(args.verify))
        print(f"Backup Verification: {result}")
        sys.exit(0 if result["verified"] else 1)

    result = perform_backup(keep_count=args.keep)
    print("Database Backup Successful:")
    print(f"  Backup DB: {result['backup_db']}")
    print(f"  Schema Version: {result['schema_version']}")
    print(f"  Tables: {result['table_count']}")
    print(f"  Integrity: {result['integrity']}")
    if result["pruned_backups"]:
        print(f"  Pruned: {result['pruned_backups']}")


if __name__ == "__main__":
    main()
