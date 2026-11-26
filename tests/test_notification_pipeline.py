from unittest.mock import Mock
import pytest
from notifications.pipeline import NotificationPipeline


def test_event_handling_only_generates_and_does_not_dispatch(monkeypatch):
    generated = {"status": "generated", "queued_or_already_queued": 1}
    generator = Mock(return_value=generated)
    monkeypatch.setattr("notifications.pipeline.generate_notifications", generator)
    worker = Mock()
    pipeline = NotificationPipeline(worker)
    payload = {"event_id": "evt-1"}
    assert pipeline.handle_shipment_change(payload) == generated
    generator.assert_called_once_with(payload)
    worker.run_once.assert_not_called()


def test_dispatch_is_explicit_and_calls_one_worker_batch():
    worker = Mock()
    worker.run_once.return_value = {"claimed": 1, "sent": 1}
    pipeline = NotificationPipeline(worker)
    assert pipeline.dispatch_pending() == {"claimed": 1, "sent": 1}
    worker.run_once.assert_called_once_with()


def test_dispatch_requires_configured_worker():
    with pytest.raises(RuntimeError, match="No delivery worker"):
        NotificationPipeline().dispatch_pending()


def test_pipeline_rejects_invalid_worker():
    with pytest.raises(TypeError):
        NotificationPipeline(object())
