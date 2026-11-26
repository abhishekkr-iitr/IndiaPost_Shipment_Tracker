"""Tracking execution with atomic shipment and checkpoint persistence."""
from __future__ import annotations

import sqlite3

from aftership_client import find_tracking_id
from database.db_connection import get_db_connection
from database.checkpoint_repository import save_checkpoints_in_transaction
from database.shipment_repository import save_shipment_in_transaction
from notifications.change_detection import detect_and_record_shipment_changes
from notifications.generation import (
    generate_notifications_in_transaction,
    generate_notifications_for_change_detection_in_transaction,
)
from database.tracking_check_repository import complete_tracking_check, start_tracking_check
from tracking.tracking_service import track_shipment


class MonitoringTrackingError(RuntimeError):
    """Raised when a monitoring tracking attempt cannot be completed safely."""


def _status_occurrence_from_checkpoints(connection, shipment_id, status):
    """Return the latest provider checkpoint timestamp matching the new status."""
    if not status:
        return None
    row = connection.execute(
        """SELECT checkpoint_id, event_date FROM checkpoints
           WHERE shipment_id = ? AND LOWER(TRIM(status)) = LOWER(TRIM(?))
             AND event_date IS NOT NULL AND TRIM(event_date) <> ''
           ORDER BY event_date DESC, checkpoint_id DESC LIMIT 1""",
        (shipment_id, status),
    ).fetchone()
    return row if row else None


def _change_ledger_available(connection):
    if not isinstance(connection, sqlite3.Connection):
        return False
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shipment_change_events'"
    ).fetchone()
    return row is not None and row[0] == 1


def execute_shipment_tracking(shipment: dict) -> dict:
    if not isinstance(shipment, dict):
        raise ValueError("shipment must be a dictionary.")
    shipment_id = shipment.get("shipment_id")
    tracking_number = shipment.get("tracking_number")
    if not isinstance(shipment_id, int) or isinstance(shipment_id, bool) or shipment_id <= 0:
        raise ValueError("shipment must contain a positive shipment_id.")
    if not isinstance(tracking_number, str) or not tracking_number.strip():
        raise ValueError("shipment must contain a non-empty tracking_number.")

    check_id = start_tracking_check(shipment_id)
    check_completed = False
    try:
        tracking_id = find_tracking_id(tracking_number.strip())
        if not tracking_id:
            message = "AfterShip tracking ID not found."
            complete_tracking_check(check_id, check_status="failed", error_message=message)
            check_completed = True
            return {"success": False, "shipment_id": shipment_id, "tracking_check_id": check_id, "error": message}

        tracking_data = track_shipment(tracking_number.strip(), tracking_id=tracking_id)
        if not isinstance(tracking_data, dict):
            message = "Unable to retrieve or normalize shipment tracking information."
            complete_tracking_check(check_id, check_status="failed", error_message=message)
            check_completed = True
            return {"success": False, "shipment_id": shipment_id, "tracking_check_id": check_id, "error": message}
        returned_number = tracking_data.get("tracking_number")
        if returned_number and (not isinstance(returned_number, str) or returned_number.strip().upper() != tracking_number.strip().upper()):
            raise MonitoringTrackingError("Provider tracking response does not match the requested shipment.")
        checkpoints = tracking_data.get("checkpoints", [])
        if not isinstance(checkpoints, list):
            raise MonitoringTrackingError("Normalized tracking checkpoints must be a list.")

        connection = get_db_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            # The worker's shipment dictionary is a pre-request snapshot. If a
            # webhook changed the canonical status while the provider request
            # was in flight, preserve that newer status rather than allowing a
            # stale monitoring response to overwrite it.
            current_row = connection.execute(
                "SELECT current_status FROM shipments WHERE shipment_id = ?",
                (shipment_id,),
            ).fetchone()
            if current_row is None:
                raise MonitoringTrackingError("Canonical shipment no longer exists.")
            snapshot_status = shipment.get("current_status")
            concurrent_status_change = current_row[0] != snapshot_status
            if concurrent_status_change:
                tracking_data = dict(tracking_data)
                tracking_data["current_status"] = current_row[0]
                shipment = dict(shipment)
                shipment["current_status"] = current_row[0]
            persisted_id = save_shipment_in_transaction(connection, tracking_data, provider_tracking_id=tracking_id)
            if persisted_id != shipment_id:
                raise MonitoringTrackingError("Tracking persistence resolved to a different canonical shipment.")
            checkpoint_save_result = save_checkpoints_in_transaction(
                connection, shipment_id, checkpoints
            )
            inserted = int(checkpoint_save_result)
            newly_inserted_checkpoints = list(
                getattr(checkpoint_save_result, "inserted_checkpoints", ())
            )

            change_detection = None
            notification_generation = None
            if _change_ledger_available(connection):
                previous_status = shipment.get("current_status")
                new_status = tracking_data.get("current_status")
                status_occurred_at = None
                status_checkpoint_id = None
                if not concurrent_status_change and new_status is not None and new_status != previous_status:
                    occurrence = _status_occurrence_from_checkpoints(
                        connection, shipment_id, new_status
                    )
                    if occurrence is not None:
                        status_checkpoint_id, status_occurred_at = occurrence
                    if status_occurred_at is None:
                        raise MonitoringTrackingError(
                            "A status transition was detected, but no provider checkpoint timestamp "
                            "matches the new status. The shipment update was rolled back rather than "
                            "inventing an occurrence timestamp."
                        )
                change_detection = detect_and_record_shipment_changes(
                    connection,
                    shipment={
                        "shipment_id": shipment_id,
                        "tracking_number": tracking_number.strip(),
                        "carrier": tracking_data.get("carrier") or shipment.get("carrier"),
                    },
                    previous_status=previous_status,
                    new_status=new_status,
                    source="monitoring",
                    new_checkpoints=newly_inserted_checkpoints,
                    status_occurred_at=status_occurred_at,
                    status_checkpoint_id=status_checkpoint_id,
                )
                notification_generation = generate_notifications_for_change_detection_in_transaction(
                    connection, change_detection
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        complete_tracking_check(check_id, check_status="success", retrieved_status=tracking_data.get("current_status"))
        check_completed = True
        return {"success": True, "shipment_id": shipment_id, "tracking_check_id": check_id,
                "tracking_id": tracking_id, "tracking_data": tracking_data,
                "inserted_checkpoints": inserted,
                "change_detection": change_detection,
                "notification_generation": notification_generation}
    except Exception as exc:
        if not check_completed:
            try:
                complete_tracking_check(check_id, check_status="failed", error_message=f"{type(exc).__name__}: {exc}")
            except Exception as completion_error:
                raise MonitoringTrackingError(
                    "Tracking execution failed and its tracking-check record could not be marked failed."
                ) from completion_error
        raise
