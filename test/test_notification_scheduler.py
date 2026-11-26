import logging
import threading
from unittest.mock import Mock

import pytest

from notifications.notification_scheduler import NotificationScheduler


def test_constructor_requires_dispatch_pipeline():
    with pytest.raises(TypeError):
        NotificationScheduler(object())


@pytest.mark.parametrize("value", [0, -1, True, "5", None])
def test_constructor_rejects_invalid_poll_interval(value):
    with pytest.raises(ValueError):
        NotificationScheduler(Mock(), polling_interval_seconds=value)


def test_run_once_delegates_to_pipeline():
    pipeline = Mock()
    pipeline.dispatch_pending.return_value = {"claimed": 2, "sent": 2}
    scheduler = NotificationScheduler(pipeline)
    assert scheduler.run_once() == {"claimed": 2, "sent": 2}
    pipeline.dispatch_pending.assert_called_once_with()


def test_run_forever_runs_immediately_then_waits_and_stops():
    pipeline = Mock()
    stop_event = Mock(spec=threading.Event)
    stop_event.is_set.side_effect = [False, False]
    stop_event.wait.return_value = True
    callback = Mock()
    scheduler = NotificationScheduler(pipeline, polling_interval_seconds=2.5)
    scheduler.run_forever(stop_event, on_cycle=callback)
    pipeline.dispatch_pending.assert_called_once_with()
    callback.assert_called_once_with(pipeline.dispatch_pending.return_value)
    stop_event.wait.assert_called_once_with(2.5)


def test_cycle_exception_is_logged_and_scheduler_continues_to_wait():
    pipeline = Mock()
    pipeline.dispatch_pending.side_effect = RuntimeError("temporary failure")
    stop_event = Mock(spec=threading.Event)
    stop_event.is_set.side_effect = [False, False]
    stop_event.wait.return_value = True
    logger = Mock(spec=logging.Logger)
    NotificationScheduler(pipeline, logger=logger).run_forever(stop_event)
    logger.exception.assert_called_once()
    stop_event.wait.assert_called_once_with(5.0)


def test_callback_exception_does_not_terminate_scheduler():
    pipeline = Mock()
    callback = Mock(side_effect=RuntimeError("callback failure"))
    stop_event = Mock(spec=threading.Event)
    stop_event.is_set.side_effect = [False, False]
    stop_event.wait.return_value = True
    logger = Mock(spec=logging.Logger)
    NotificationScheduler(pipeline, logger=logger).run_forever(
        stop_event, on_cycle=callback
    )
    logger.exception.assert_called_once()
    stop_event.wait.assert_called_once_with(5.0)


def test_already_stopped_event_does_not_dispatch():
    pipeline = Mock()
    stop_event = threading.Event()
    stop_event.set()
    NotificationScheduler(pipeline).run_forever(stop_event)
    pipeline.dispatch_pending.assert_not_called()


def test_invalid_stop_event_is_rejected():
    with pytest.raises(TypeError):
        NotificationScheduler(Mock()).run_forever(object())
