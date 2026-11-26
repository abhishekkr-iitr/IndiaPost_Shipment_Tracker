"""Focused tests for monitoring scheduler project-relative .env loading."""
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import run_monitoring_scheduler


class TestMonitoringSchedulerEnvironmentLoading(unittest.TestCase):
    def test_main_loads_project_env_without_overriding_os_environment(self):
        expected_path = Path(run_monitoring_scheduler.__file__).resolve().parent / ".env"
        with patch.object(run_monitoring_scheduler, "load_dotenv") as load_dotenv, \
             patch.object(run_monitoring_scheduler.logging, "basicConfig"), \
             patch.object(run_monitoring_scheduler, "MonitoringWorker") as worker_cls, \
             patch.object(run_monitoring_scheduler, "MonitoringScheduler") as scheduler_cls, \
             patch.object(run_monitoring_scheduler.signal, "signal"), \
             patch.object(run_monitoring_scheduler.threading, "Event") as event_cls:
            scheduler = scheduler_cls.return_value
            scheduler.polling_interval_seconds = 60
            event_cls.return_value = threading.Event()
            run_monitoring_scheduler.main()
        load_dotenv.assert_called_once_with(dotenv_path=expected_path, override=False)
        worker_cls.assert_called_once()
        scheduler_cls.assert_called_once()
        scheduler.run_forever.assert_called_once()


if __name__ == "__main__":
    unittest.main()
