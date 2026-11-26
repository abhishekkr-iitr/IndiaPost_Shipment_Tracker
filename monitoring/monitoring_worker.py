"""Reliable orchestration for proactive shipment monitoring."""
from __future__ import annotations

import threading

from database.db_connection import get_db_connection
from database.monitoring_job_repository import (
    claim_due_monitoring_jobs,
    recover_expired_monitoring_job_leases,
    renew_monitoring_job_lease,
)
from monitoring.job_outcomes import complete_monitoring_cycle, record_monitoring_failure
from monitoring.tracking_executor import execute_shipment_tracking


class MonitoringWorkerError(RuntimeError):
    """Raised when a claimed monitoring job cannot be prepared safely."""


class _LeaseHeartbeat:
    """Renew every claimed lease while a batch is being processed."""
    def __init__(self, jobs, worker_id, lease_seconds):
        self.jobs = jobs
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.lost = set()
        self.errors = {}
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        interval = max(0.25, self.lease_seconds / 3.0)
        def run():
            while not self.stop_event.wait(interval):
                for job in self.jobs:
                    job_id = job.get("monitoring_job_id") if isinstance(job, dict) else None
                    if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id <= 0:
                        continue
                    if job_id in self.lost:
                        continue
                    try:
                        renewed = renew_monitoring_job_lease(
                            job_id, self.worker_id, lease_seconds=self.lease_seconds
                        )
                        if not renewed:
                            self.lost.add(job_id)
                    except Exception as exc:  # keep retrying until ownership can be confirmed
                        self.errors[job_id] = f"{type(exc).__name__}: {exc}"
        self.thread = threading.Thread(target=run, name="monitoring-lease-heartbeat", daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=max(1.0, self.lease_seconds / 3.0 + 0.5))

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.stop()


class MonitoringWorker:
    """Claim and execute shipment-level monitoring jobs."""
    def __init__(self, worker_id: str, *, batch_size: int = 1, lease_seconds: int = 300):
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be a non-empty string.")
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer.")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive integer.")
        self.worker_id = worker_id.strip()
        self.batch_size = batch_size
        self.lease_seconds = lease_seconds

    def claim_due_jobs(self, *, now=None) -> list[dict]:
        # Return abandoned processing jobs to the retry queue before claiming.
        recover_expired_monitoring_job_leases(now=now)
        return claim_due_monitoring_jobs(
            self.worker_id, limit=self.batch_size, lease_seconds=self.lease_seconds, now=now
        )

    @staticmethod
    def get_shipment_details(job: dict) -> dict:
        if not isinstance(job, dict):
            raise ValueError("job must be a dictionary.")
        job_id, shipment_id = job.get("monitoring_job_id"), job.get("shipment_id")
        if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id <= 0:
            raise ValueError("job must contain a positive monitoring_job_id.")
        if not isinstance(shipment_id, int) or isinstance(shipment_id, bool) or shipment_id <= 0:
            raise ValueError("job must contain a positive shipment_id.")
        connection = get_db_connection()
        try:
            row = connection.execute(
                "SELECT shipment_id, tracking_number, carrier, current_status FROM shipments WHERE shipment_id = ?",
                (shipment_id,),
            ).fetchone()
            if row is None:
                raise MonitoringWorkerError(
                    f"Shipment {shipment_id} for monitoring job {job_id} does not exist."
                )
            return {"shipment_id": row[0], "tracking_number": row[1], "carrier": row[2], "current_status": row[3]}
        finally:
            connection.close()

    def run_once(self, *, now=None) -> list[dict]:
        """Backward-compatible claim-and-prepare operation; does not execute."""
        jobs = self.claim_due_jobs(now=now)
        return [{"job": job, "shipment": self.get_shipment_details(job)} for job in jobs]

    def _record_failure_safely(self, job_id, error, *, now=None):
        try:
            return record_monitoring_failure(
                job_id, self.worker_id, error=f"{type(error).__name__}: {error}", now=now
            )
        except Exception as outcome_error:
            return {"recorded": False, "reason": "outcome_transition_failed",
                    "error": f"{type(outcome_error).__name__}: {outcome_error}"}

    def run_and_process_once(self, *, now=None) -> list[dict]:
        """Claim, track, persist and transition jobs while renewing their leases."""
        results = []
        jobs = self.claim_due_jobs(now=now)
        if not jobs:
            return results

        with _LeaseHeartbeat(jobs, self.worker_id, self.lease_seconds) as heartbeat:
            for job in jobs:
                job_id = job.get("monitoring_job_id") if isinstance(job, dict) else None
                if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id <= 0:
                    results.append({"job_id": job_id, "tracking_success": False,
                                    "error": "Claimed monitoring job has an invalid monitoring_job_id.",
                                    "job_outcome": {"recorded": False, "reason": "invalid_claimed_job"}})
                    continue
                if job_id in heartbeat.lost:
                    results.append({"job_id": job_id, "tracking_success": False,
                                    "job_outcome": {"completed": False, "reason": "lease_lost"}})
                    continue
                try:
                    shipment = self.get_shipment_details(job)
                    tracking_result = execute_shipment_tracking(shipment)
                    if job_id in heartbeat.lost:
                        results.append({"job_id": job_id, "tracking_success": False,
                                        "job_outcome": {"completed": False, "reason": "lease_lost"}})
                        continue
                    if not isinstance(tracking_result, dict) or not tracking_result.get("success"):
                        error = (tracking_result.get("error", "Tracking attempt failed.")
                                 if isinstance(tracking_result, dict) else "Tracking executor returned an invalid result.")
                        outcome = record_monitoring_failure(job_id, self.worker_id, error=error, now=now)
                        results.append({"job_id": job_id, "tracking_success": False, "job_outcome": outcome})
                        continue
                    tracking_data = tracking_result.get("tracking_data")
                    if not isinstance(tracking_data, dict):
                        raise MonitoringWorkerError("Successful tracking result did not contain tracking_data.")
                    outcome = complete_monitoring_cycle(
                        job_id, self.worker_id, shipment_status=tracking_data.get("current_status"), now=now
                    )
                    results.append({"job_id": job_id, "tracking_success": True,
                                    "tracking_check_id": tracking_result.get("tracking_check_id"),
                                    "inserted_checkpoints": tracking_result.get("inserted_checkpoints", 0),
                                    "job_outcome": outcome})
                except Exception as exc:
                    outcome = self._record_failure_safely(job_id, exc, now=now)
                    results.append({"job_id": job_id, "tracking_success": False,
                                    "error": f"{type(exc).__name__}: {exc}", "job_outcome": outcome})
        return results
