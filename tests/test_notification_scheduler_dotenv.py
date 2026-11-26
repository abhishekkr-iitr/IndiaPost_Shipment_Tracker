"""Focused tests for notification scheduler project-relative .env loading."""
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import run_notification_scheduler


class TestNotificationSchedulerEnvironmentLoading(unittest.TestCase):
    def test_main_loads_project_env_without_overriding_os_environment(self):
        expected_path = Path(run_notification_scheduler.__file__).resolve().parent / ".env"
        with patch.object(run_notification_scheduler, "load_dotenv") as load_dotenv, \
             patch.object(run_notification_scheduler.logging, "basicConfig"), \
             patch.object(run_notification_scheduler, "build_delivery_runtime") as runtime_builder, \
             patch.object(run_notification_scheduler, "NotificationWorker") as worker_cls, \
             patch.object(run_notification_scheduler, "NotificationPipeline") as pipeline_cls, \
             patch.object(run_notification_scheduler, "NotificationScheduler") as scheduler_cls, \
             patch.object(run_notification_scheduler.signal, "signal"), \
             patch.object(run_notification_scheduler.threading, "Event") as event_cls:
            runtime = runtime_builder.return_value
            runtime.registry = object()
            scheduler = scheduler_cls.return_value
            scheduler.polling_interval_seconds = 5
            event_cls.return_value = threading.Event()
            run_notification_scheduler.main()
        load_dotenv.assert_called_once_with(dotenv_path=expected_path, override=False)
        runtime_builder.assert_called_once()
        worker_cls.assert_called_once()
        pipeline_cls.assert_called_once()
        scheduler_cls.assert_called_once()
        scheduler.run_forever.assert_called_once()
        runtime.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
