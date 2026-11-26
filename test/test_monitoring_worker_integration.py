"""Step 5.4.4 integration tests for the monitoring worker orchestration."""

import unittest
from unittest.mock import call, patch

from monitoring.monitoring_worker import MonitoringWorker


NOW = "2026-11-16T12:00:00+00:00"


class TestMonitoringWorkerIntegration(unittest.TestCase):
    def setUp(self):
        self.worker = MonitoringWorker("integration-worker", batch_size=3)
        self.jobs = [
            {"monitoring_job_id": 11, "shipment_id": 101},
            {"monitoring_job_id": 12, "shipment_id": 102},
            {"monitoring_job_id": 13, "shipment_id": 103},
        ]

    @patch("monitoring.monitoring_worker.complete_monitoring_cycle")
    @patch("monitoring.monitoring_worker.execute_shipment_tracking")
    @patch.object(MonitoringWorker, "get_shipment_details")
    @patch.object(MonitoringWorker, "claim_due_jobs")
    def test_end_to_end_success_path_for_claimed_job(
        self, claim, details, execute, complete
    ):
        claim.return_value = [self.jobs[0]]
        details.return_value = {
            "shipment_id": 101,
            "tracking_number": "AB123456789IN",
            "carrier": "india-post",
            "current_status": "In Transit",
        }
        execute.return_value = {
            "success": True,
            "tracking_check_id": 501,
            "tracking_data": {"current_status": "In Transit"},
            "inserted_checkpoints": 2,
        }
        complete.return_value = {
            "completed": True,
            "status": "scheduled",
            "next_run_at": "2026-11-16T18:00:00+00:00",
        }

        result = self.worker.run_and_process_once(now=NOW)

        claim.assert_called_once_with(now=NOW)
        details.assert_called_once_with(self.jobs[0])
        execute.assert_called_once_with(details.return_value)
        complete.assert_called_once_with(
            11, "integration-worker", shipment_status="In Transit", now=NOW
        )
        self.assertEqual(result[0]["tracking_check_id"], 501)
        self.assertTrue(result[0]["tracking_success"])

    @patch("monitoring.monitoring_worker.record_monitoring_failure")
    @patch("monitoring.monitoring_worker.execute_shipment_tracking")
    @patch.object(MonitoringWorker, "get_shipment_details")
    @patch.object(MonitoringWorker, "claim_due_jobs")
    def test_provider_failure_transitions_job_to_retry_management(
        self, claim, details, execute, record_failure
    ):
        claim.return_value = [self.jobs[0]]
        details.return_value = {"shipment_id": 101, "tracking_number": "AB123456789IN"}
        execute.return_value = {"success": False, "error": "provider returned no record"}
        record_failure.return_value = {
            "recorded": True,
            "exhausted": False,
            "next_retry_at": "2026-11-16T12:05:00+00:00",
        }

        result = self.worker.run_and_process_once(now=NOW)

        record_failure.assert_called_once_with(
            11, "integration-worker",
            error="provider returned no record", now=NOW
        )
        self.assertFalse(result[0]["tracking_success"])
        self.assertTrue(result[0]["job_outcome"]["recorded"])

    @patch("monitoring.monitoring_worker.record_monitoring_failure")
    @patch.object(MonitoringWorker, "get_shipment_details", side_effect=RuntimeError("shipment lookup failed"))
    @patch.object(MonitoringWorker, "claim_due_jobs")
    def test_shipment_lookup_failure_does_not_strand_claimed_job(
        self, claim, details, record_failure
    ):
        claim.return_value = [self.jobs[0]]
        record_failure.return_value = {"recorded": True}

        result = self.worker.run_and_process_once(now=NOW)

        record_failure.assert_called_once()
        self.assertIn("shipment lookup failed", result[0]["error"])
        self.assertTrue(result[0]["job_outcome"]["recorded"])

    @patch("monitoring.monitoring_worker.record_monitoring_failure")
    @patch("monitoring.monitoring_worker.execute_shipment_tracking")
    @patch.object(MonitoringWorker, "get_shipment_details")
    @patch.object(MonitoringWorker, "claim_due_jobs")
    def test_one_job_failure_does_not_prevent_processing_remaining_batch(
        self, claim, details, execute, record_failure
    ):
        claim.return_value = self.jobs
        details.side_effect = [
            {"shipment_id": 101, "tracking_number": "A"},
            {"shipment_id": 102, "tracking_number": "B"},
            {"shipment_id": 103, "tracking_number": "C"},
        ]
        execute.side_effect = [
            {"success": False, "error": "temporary error"},
            {"success": True, "tracking_data": {"current_status": "In Transit"}},
            {"success": False, "error": "another temporary error"},
        ]
        record_failure.return_value = {"recorded": True}

        with patch("monitoring.monitoring_worker.complete_monitoring_cycle", return_value={"completed": True}):
            result = self.worker.run_and_process_once(now=NOW)

        self.assertEqual(len(result), 3)
        self.assertEqual(execute.call_count, 3)
        self.assertEqual(record_failure.call_count, 2)
        self.assertTrue(result[1]["tracking_success"])

    @patch("monitoring.monitoring_worker.record_monitoring_failure")
    @patch("monitoring.monitoring_worker.execute_shipment_tracking", return_value={"success": True})
    @patch.object(MonitoringWorker, "get_shipment_details")
    @patch.object(MonitoringWorker, "claim_due_jobs")
    def test_malformed_success_result_is_recorded_as_failure(
        self, claim, details, execute, record_failure
    ):
        claim.return_value = [self.jobs[0]]
        details.return_value = {"shipment_id": 101, "tracking_number": "A"}
        record_failure.return_value = {"recorded": True}

        result = self.worker.run_and_process_once(now=NOW)

        self.assertFalse(result[0]["tracking_success"])
        self.assertIn("tracking_data", result[0]["error"])
        record_failure.assert_called_once()

    @patch("monitoring.monitoring_worker.record_monitoring_failure", side_effect=RuntimeError("outcome DB unavailable"))
    @patch.object(MonitoringWorker, "get_shipment_details", side_effect=RuntimeError("lookup failed"))
    @patch.object(MonitoringWorker, "claim_due_jobs")
    def test_failure_to_record_failure_is_reported_without_crashing_batch(
        self, claim, details, record_failure
    ):
        claim.return_value = [self.jobs[0]]
        result = self.worker.run_and_process_once(now=NOW)

        self.assertFalse(result[0]["job_outcome"]["recorded"])
        self.assertEqual(
            result[0]["job_outcome"]["reason"], "outcome_transition_failed"
        )

    @patch("monitoring.monitoring_worker.complete_monitoring_cycle", return_value={"completed": True, "delivered": True})
    @patch("monitoring.monitoring_worker.execute_shipment_tracking")
    @patch.object(MonitoringWorker, "get_shipment_details")
    @patch.object(MonitoringWorker, "claim_due_jobs")
    def test_delivered_tracking_result_is_forwarded_to_completion_policy(
        self, claim, details, execute, complete
    ):
        claim.return_value = [self.jobs[0]]
        details.return_value = {"shipment_id": 101, "tracking_number": "A"}
        execute.return_value = {
            "success": True,
            "tracking_data": {"current_status": "Delivered"},
            "tracking_check_id": 700,
            "inserted_checkpoints": 1,
        }

        result = self.worker.run_and_process_once(now=NOW)

        complete.assert_called_once_with(
            11, "integration-worker", shipment_status="Delivered", now=NOW
        )
        self.assertTrue(result[0]["tracking_success"])
        self.assertTrue(result[0]["job_outcome"]["delivered"])


if __name__ == "__main__":
    unittest.main()
