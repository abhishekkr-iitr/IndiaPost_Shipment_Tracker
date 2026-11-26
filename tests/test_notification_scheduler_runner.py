import pytest

from run_notification_scheduler import (
    _nonnegative_int_env,
    _positive_float_env,
    _positive_int_env,
)


def test_positive_int_env_uses_default_and_parses_value(monkeypatch):
    monkeypatch.delenv("NOTIFICATION_BATCH_SIZE", raising=False)
    assert _positive_int_env("NOTIFICATION_BATCH_SIZE", 20) == 20
    monkeypatch.setenv("NOTIFICATION_BATCH_SIZE", "7")
    assert _positive_int_env("NOTIFICATION_BATCH_SIZE", 20) == 7


@pytest.mark.parametrize("value", ["0", "-1", "bad"])
def test_positive_int_env_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("NOTIFICATION_BATCH_SIZE", value)
    with pytest.raises(ValueError):
        _positive_int_env("NOTIFICATION_BATCH_SIZE", 20)


def test_retry_delay_allows_zero(monkeypatch):
    monkeypatch.setenv("NOTIFICATION_RETRY_DELAY_SECONDS", "0")
    assert _nonnegative_int_env("NOTIFICATION_RETRY_DELAY_SECONDS", 60) == 0


@pytest.mark.parametrize("value", ["-1", "bad"])
def test_retry_delay_rejects_negative_or_invalid_values(monkeypatch, value):
    monkeypatch.setenv("NOTIFICATION_RETRY_DELAY_SECONDS", value)
    with pytest.raises(ValueError):
        _nonnegative_int_env("NOTIFICATION_RETRY_DELAY_SECONDS", 60)


def test_positive_float_rejects_non_finite(monkeypatch):
    monkeypatch.setenv("NOTIFICATION_SCHEDULER_POLL_SECONDS", "inf")
    with pytest.raises(ValueError):
        _positive_float_env("NOTIFICATION_SCHEDULER_POLL_SECONDS", 5.0)
