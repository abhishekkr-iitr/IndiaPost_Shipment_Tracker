"""Focused tests for the lightweight monitoring polling scheduler."""
import logging
import threading
import unittest
from unittest.mock import Mock

from monitoring.monitoring_scheduler import MonitoringScheduler


class TestMonitoringScheduler(unittest.TestCase):
    def test_constructor_requires_worker_cycle_method(self):
        with self.assertRaises(TypeError):
            MonitoringScheduler(object())

    def test_constructor_validates_poll_interval(self):
        worker = Mock()
        for invalid in (0, -1, True, "5", None):
            with self.subTest(value=invalid):
                with self.assertRaises(ValueError):
                    MonitoringScheduler(worker, polling_interval_seconds=invalid)

    def test_run_once_delegates_to_existing_worker(self):
        worker = Mock()
        worker.run_and_process_once.return_value = [{"job_id": 7, "tracking_success": True}]
        scheduler = MonitoringScheduler(worker, polling_interval_seconds=15)

        result = scheduler.run_once()

        self.assertEqual(result, [{"job_id": 7, "tracking_success": True}])
        worker.run_and_process_once.assert_called_once_with()

    def test_run_forever_runs_immediately_then_stops_after_first_wait(self):
        worker = Mock()
        stop_event = Mock(spec=threading.Event)
        stop_event.is_set.side_effect = [False, False]
        stop_event.wait.return_value = True
        on_cycle = Mock()
        scheduler = MonitoringScheduler(worker, polling_interval_seconds=2.5)

        scheduler.run_forever(stop_event, on_cycle=on_cycle)

        worker.run_and_process_once.assert_called_once_with()
        on_cycle.assert_called_once_with(worker.run_and_process_once.return_value)
        stop_event.wait.assert_called_once_with(2.5)

    def test_cycle_exception_is_logged_and_scheduler_still_waits(self):
        worker = Mock()
        worker.run_and_process_once.side_effect = RuntimeError("temporary failure")
        stop_event = Mock(spec=threading.Event)
        stop_event.is_set.side_effect = [False, False]
        stop_event.wait.return_value = True
        logger = Mock(spec=logging.Logger)
        scheduler = MonitoringScheduler(worker, polling_interval_seconds=3, logger=logger)

        scheduler.run_forever(stop_event)

        logger.exception.assert_called_once()
        stop_event.wait.assert_called_once_with(3.0)

    def test_already_stopped_event_does_not_invoke_worker(self):
        worker = Mock()
        stop_event = threading.Event()
        stop_event.set()

        MonitoringScheduler(worker).run_forever(stop_event)

        worker.run_and_process_once.assert_not_called()

    def test_invalid_stop_event_is_rejected(self):
        with self.assertRaises(TypeError):
            MonitoringScheduler(Mock()).run_forever(stop_event=object())


if __name__ == "__main__":
    unittest.main()
