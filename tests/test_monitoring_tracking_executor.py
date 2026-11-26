"""Tests for transactional monitoring tracking persistence."""
import unittest
from unittest.mock import MagicMock, patch
from monitoring.tracking_executor import MonitoringTrackingError, execute_shipment_tracking

class TestMonitoringTrackingExecutor(unittest.TestCase):
    def setUp(self):
        self.shipment = {"shipment_id": 8, "tracking_number": "TEST456789123IN", "carrier": "india-post", "current_status": None}
        self.data = {"tracking_number": "TEST456789123IN", "carrier": "india-post", "current_status": "Out for Delivery", "checkpoints": [{"date": "2026-11-16T10:00:00+00:00"}]}

    @patch("monitoring.tracking_executor.complete_tracking_check")
    @patch("monitoring.tracking_executor.save_checkpoints_in_transaction", return_value=1)
    @patch("monitoring.tracking_executor.save_shipment_in_transaction", return_value=8)
    @patch("monitoring.tracking_executor.get_db_connection")
    @patch("monitoring.tracking_executor.track_shipment")
    @patch("monitoring.tracking_executor.find_tracking_id", return_value="provider-id")
    @patch("monitoring.tracking_executor.start_tracking_check", return_value=42)
    def test_success_persists_atomically(self, start, find_id, track, get_connection, save_shipment, save_points, complete):
        con=MagicMock(); con.execute.return_value.fetchone.return_value=(None,); get_connection.return_value=con; track.return_value=self.data
        result=execute_shipment_tracking(self.shipment)
        self.assertTrue(result["success"]); self.assertEqual(result["tracking_check_id"],42)
        self.assertEqual(con.execute.call_args_list[0].args, ("BEGIN IMMEDIATE",)); self.assertEqual(con.execute.call_count, 2); con.commit.assert_called_once()
        save_shipment.assert_called_once_with(con,self.data,provider_tracking_id="provider-id")
        save_points.assert_called_once_with(con,8,self.data["checkpoints"])
        complete.assert_called_once_with(42,check_status="success",retrieved_status="Out for Delivery")

    @patch("monitoring.tracking_executor.complete_tracking_check")
    @patch("monitoring.tracking_executor.find_tracking_id", return_value=None)
    @patch("monitoring.tracking_executor.start_tracking_check", return_value=43)
    def test_missing_provider_id_marks_check_failed(self,start,find_id,complete):
        result=execute_shipment_tracking(self.shipment)
        self.assertFalse(result["success"]); complete.assert_called_once_with(43,check_status="failed",error_message="AfterShip tracking ID not found.")

    @patch("monitoring.tracking_executor.complete_tracking_check")
    @patch("monitoring.tracking_executor.track_shipment",return_value=None)
    @patch("monitoring.tracking_executor.find_tracking_id",return_value="provider-id")
    @patch("monitoring.tracking_executor.start_tracking_check",return_value=44)
    def test_unavailable_tracking_data_marks_check_failed(self,start,find_id,track,complete):
        result=execute_shipment_tracking(self.shipment)
        self.assertFalse(result["success"]); self.assertEqual(complete.call_args.kwargs["check_status"],"failed")

    @patch("monitoring.tracking_executor.complete_tracking_check")
    @patch("monitoring.tracking_executor.track_shipment",side_effect=TimeoutError("provider timeout"))
    @patch("monitoring.tracking_executor.find_tracking_id",return_value="provider-id")
    @patch("monitoring.tracking_executor.start_tracking_check",return_value=45)
    def test_provider_exception_marks_check_failed_and_reraises(self,start,find_id,track,complete):
        with self.assertRaisesRegex(TimeoutError,"provider timeout"): execute_shipment_tracking(self.shipment)
        self.assertEqual(complete.call_args.kwargs["check_status"],"failed")

    @patch("monitoring.tracking_executor.complete_tracking_check")
    @patch("monitoring.tracking_executor.save_checkpoints_in_transaction")
    @patch("monitoring.tracking_executor.save_shipment_in_transaction",return_value=8)
    @patch("monitoring.tracking_executor.get_db_connection")
    @patch("monitoring.tracking_executor.track_shipment")
    @patch("monitoring.tracking_executor.find_tracking_id",return_value="provider-id")
    @patch("monitoring.tracking_executor.start_tracking_check",return_value=46)
    def test_checkpoint_failure_rolls_back(self,start,find_id,track,get_connection,save_shipment,save_points,complete):
        con=MagicMock(); get_connection.return_value=con; track.return_value=self.data
        save_points.side_effect=RuntimeError("checkpoint failure")
        with self.assertRaisesRegex(RuntimeError,"checkpoint failure"): execute_shipment_tracking(self.shipment)
        con.rollback.assert_called_once(); con.commit.assert_not_called()
        self.assertEqual(complete.call_args.kwargs["check_status"],"failed")

    @patch("monitoring.tracking_executor.complete_tracking_check")
    @patch("monitoring.tracking_executor.save_checkpoints_in_transaction")
    @patch("monitoring.tracking_executor.save_shipment_in_transaction",return_value=99)
    @patch("monitoring.tracking_executor.get_db_connection")
    @patch("monitoring.tracking_executor.track_shipment")
    @patch("monitoring.tracking_executor.find_tracking_id",return_value="provider-id")
    @patch("monitoring.tracking_executor.start_tracking_check",return_value=47)
    def test_different_canonical_shipment_is_rejected(self,start,find_id,track,get_connection,save_shipment,save_points,complete):
        con=MagicMock(); get_connection.return_value=con; track.return_value=self.data
        with self.assertRaises(MonitoringTrackingError): execute_shipment_tracking(self.shipment)
        con.rollback.assert_called_once(); save_points.assert_not_called()

    @patch("monitoring.tracking_executor.complete_tracking_check")
    @patch("monitoring.tracking_executor.track_shipment")
    @patch("monitoring.tracking_executor.find_tracking_id",return_value="provider-id")
    @patch("monitoring.tracking_executor.start_tracking_check",return_value=48)
    def test_tracking_number_mismatch_rejected(self,start,find_id,track,complete):
        self.data["tracking_number"]="WRONG123456789"; track.return_value=self.data
        with self.assertRaises(MonitoringTrackingError): execute_shipment_tracking(self.shipment)
        complete.assert_called_once(); self.assertEqual(complete.call_args.kwargs["check_status"],"failed")

    def test_invalid_shipment_input_rejected(self):
        with self.assertRaises(ValueError): execute_shipment_tracking({"shipment_id":0,"tracking_number":"X"})
        with self.assertRaises(ValueError): execute_shipment_tracking({"shipment_id":8,"tracking_number":" "})

if __name__ == "__main__": unittest.main()
