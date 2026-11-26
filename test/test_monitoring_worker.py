"""Focused tests for the Stage 10 Step 5.4.1 monitoring worker core."""

import unittest
from unittest.mock import MagicMock, patch

from monitoring.monitoring_worker import MonitoringWorker, MonitoringWorkerError


class TestMonitoringWorker(unittest.TestCase):
    def test_constructor_validates_worker_id_and_limits(self):
        with self.assertRaises(ValueError):
            MonitoringWorker(" ")
        with self.assertRaises(ValueError):
            MonitoringWorker("worker-1", batch_size=0)
        with self.assertRaises(ValueError):
            MonitoringWorker("worker-1", lease_seconds=0)

    @patch("monitoring.monitoring_worker.claim_due_monitoring_jobs")
    @patch("monitoring.monitoring_worker.recover_expired_monitoring_job_leases")
    def test_claim_recovers_expired_leases_before_delegating_to_repository(self, recover, claim):
        claim.return_value = [{"monitoring_job_id": 4, "shipment_id": 8}]
        worker = MonitoringWorker("worker-A", batch_size=3, lease_seconds=90)

        result = worker.claim_due_jobs(now="2026-11-16T10:00:00+00:00")

        self.assertEqual(result, claim.return_value)
        recover.assert_called_once_with(now="2026-11-16T10:00:00+00:00")
        claim.assert_called_once_with(
            "worker-A",
            limit=3,
            lease_seconds=90,
            now="2026-11-16T10:00:00+00:00",
        )

    @patch("monitoring.monitoring_worker.claim_due_monitoring_jobs")
    @patch("monitoring.monitoring_worker.recover_expired_monitoring_job_leases")
    def test_expired_lease_recovery_runs_before_job_claiming(self, recover, claim):
        calls = []
        recover.side_effect = lambda **kwargs: calls.append("recover")
        claim.side_effect = lambda *args, **kwargs: calls.append("claim") or []

        MonitoringWorker("worker-A").claim_due_jobs(now="2026-11-16T10:00:00+00:00")

        self.assertEqual(calls, ["recover", "claim"])


    @patch("monitoring.monitoring_worker.get_db_connection")
    def test_resolves_one_canonical_shipment_without_subscription_lookup(self, get_connection):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = (
            8, "TEST456789123IN", "india-post", "In Transit"
        )
        get_connection.return_value = connection

        result = MonitoringWorker.get_shipment_details(
            {"monitoring_job_id": 4, "shipment_id": 8}
        )

        self.assertEqual(result, {
            "shipment_id": 8,
            "tracking_number": "TEST456789123IN",
            "carrier": "india-post",
            "current_status": "In Transit",
        })
        sql = connection.execute.call_args.args[0].lower()
        self.assertIn("from shipments", sql)
        self.assertNotIn("user_shipments", sql)
        connection.close.assert_called_once()

    @patch("monitoring.monitoring_worker.get_db_connection")
    def test_missing_shipment_raises_and_closes_connection(self, get_connection):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = None
        get_connection.return_value = connection

        with self.assertRaises(MonitoringWorkerError):
            MonitoringWorker.get_shipment_details(
                {"monitoring_job_id": 4, "shipment_id": 8}
            )
        connection.close.assert_called_once()

    @patch.object(MonitoringWorker, "get_shipment_details")
    @patch.object(MonitoringWorker, "claim_due_jobs")
    def test_run_once_prepares_each_claimed_shipment(self, claim, details):
        job = {"monitoring_job_id": 4, "shipment_id": 8}
        shipment = {"shipment_id": 8, "tracking_number": "TEST456789123IN"}
        claim.return_value = [job]
        details.return_value = shipment

        result = MonitoringWorker("worker-A").run_once()

        self.assertEqual(result, [{"job": job, "shipment": shipment}])
        details.assert_called_once_with(job)

    @patch.object(MonitoringWorker, "claim_due_jobs", return_value=[])
    def test_run_once_returns_empty_when_no_jobs_are_due(self, claim):
        result = MonitoringWorker("worker-A").run_once()
        self.assertEqual(result, [])
        claim.assert_called_once_with(now=None)


if __name__ == "__main__":
    unittest.main()
