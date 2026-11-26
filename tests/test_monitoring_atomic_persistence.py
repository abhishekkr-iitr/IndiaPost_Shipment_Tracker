import unittest
from unittest.mock import MagicMock, patch
from monitoring.tracking_executor import execute_shipment_tracking

class TestAtomicMonitoringPersistence(unittest.TestCase):
    @patch("monitoring.tracking_executor.complete_tracking_check")
    @patch("monitoring.tracking_executor.save_checkpoints_in_transaction", return_value=2)
    @patch("monitoring.tracking_executor.save_shipment_in_transaction", return_value=8)
    @patch("monitoring.tracking_executor.get_db_connection")
    @patch("monitoring.tracking_executor.track_shipment")
    @patch("monitoring.tracking_executor.find_tracking_id", return_value="provider-id")
    @patch("monitoring.tracking_executor.start_tracking_check", return_value=99)
    def test_shipment_and_checkpoints_share_one_transaction(
        self, start, find_id, track, get_connection, save_shipment, save_checkpoints, complete
    ):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = ("Booked",)
        get_connection.return_value = connection
        track.return_value = {
            "tracking_number": "TEST456789123IN", "carrier": "india-post",
            "current_status": "In Transit", "checkpoints": [{"status": "In Transit"}],
        }
        result = execute_shipment_tracking({"shipment_id": 8, "tracking_number": "TEST456789123IN", "current_status": "Booked"})
        self.assertEqual(connection.execute.call_args_list[0].args, ("BEGIN IMMEDIATE",))
        self.assertEqual(connection.execute.call_count, 2)
        save_shipment.assert_called_once_with(connection, track.return_value, provider_tracking_id="provider-id")
        save_checkpoints.assert_called_once_with(connection, 8, track.return_value["checkpoints"])
        connection.commit.assert_called_once()
        connection.close.assert_called_once()
        self.assertEqual(result["inserted_checkpoints"], 2)

    @patch("monitoring.tracking_executor.complete_tracking_check")
    @patch("monitoring.tracking_executor.save_checkpoints_in_transaction", side_effect=RuntimeError("checkpoint write failed"))
    @patch("monitoring.tracking_executor.save_shipment_in_transaction", return_value=8)
    @patch("monitoring.tracking_executor.get_db_connection")
    @patch("monitoring.tracking_executor.track_shipment")
    @patch("monitoring.tracking_executor.find_tracking_id", return_value="provider-id")
    @patch("monitoring.tracking_executor.start_tracking_check", return_value=100)
    def test_checkpoint_failure_rolls_back_shipment_transaction(
        self, start, find_id, track, get_connection, save_shipment, save_checkpoints, complete
    ):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = ("Booked",)
        get_connection.return_value = connection
        track.return_value = {"tracking_number": "TEST456789123IN", "carrier": "india-post",
                              "current_status": "In Transit", "checkpoints": []}
        with self.assertRaisesRegex(RuntimeError, "checkpoint write failed"):
            execute_shipment_tracking({"shipment_id": 8, "tracking_number": "TEST456789123IN", "current_status": "Booked"})
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()
        complete.assert_called_once()
        self.assertEqual(complete.call_args.kwargs["check_status"], "failed")

if __name__ == "__main__":
    unittest.main()
