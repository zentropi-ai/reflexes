"""Tests for the OpenTelemetry observability scaffolding.

The module is opt-in via ``pip install reflexes[otel]``. Without the
extra installed, every public function MUST be a no-op so users who
don't want OTel pay zero runtime cost. With it installed, calls flow
through to the OTel API. Both paths are tested here regardless of
whether opentelemetry is currently importable.
"""
from __future__ import annotations

import logging

import pytest


@pytest.fixture
def fresh_obs(monkeypatch):
    """Reset the observability module's lazy caches between tests."""
    import reflexes.observability as obs
    monkeypatch.setattr(obs, "_OTEL_AVAILABLE", None)
    monkeypatch.setattr(obs, "_meter", None)
    monkeypatch.setattr(obs, "_tracer", None)
    monkeypatch.setattr(obs, "_verdicts_counter", None)
    monkeypatch.setattr(obs, "_latency_histogram", None)
    return obs


@pytest.fixture
def force_otel_unavailable(monkeypatch, fresh_obs):
    """Force _check_otel() to return False regardless of import state."""
    monkeypatch.setattr(fresh_obs, "_OTEL_AVAILABLE", False)
    return fresh_obs


# ---- No-op contract (otel unavailable) ---------------------------------


def test_check_otel_returns_false_when_unavailable(force_otel_unavailable):
    obs = force_otel_unavailable
    assert obs._check_otel() is False


def test_evaluator_span_yields_none_when_otel_unavailable(force_otel_unavailable):
    obs = force_otel_unavailable
    with obs.evaluator_span(policy_name="p", evaluator_type="t") as span:
        assert span is None


def test_record_verdict_noop_when_span_is_none(force_otel_unavailable):
    obs = force_otel_unavailable
    # Must not raise.
    obs.record_verdict(None, label=1, confidence=0.95, latency_ms=50.0)


def test_record_evaluator_error_noop_when_span_is_none(force_otel_unavailable):
    obs = force_otel_unavailable
    obs.record_evaluator_error(None, error_type="TimeoutError")


def test_increment_verdict_counter_noop_when_otel_unavailable(force_otel_unavailable):
    obs = force_otel_unavailable
    # Must not raise.
    obs.increment_verdict_counter("deny")
    obs.increment_verdict_counter("flag")
    obs.increment_verdict_counter("log")
    obs.increment_verdict_counter("error")
    obs.increment_verdict_counter("no_match")


def test_record_latency_noop_when_otel_unavailable(force_otel_unavailable):
    obs = force_otel_unavailable
    obs.record_latency("ZentropiEvaluator", 123.45)


# ---- Functional path (otel available — current env has it transitively) ----


def test_evaluator_span_yields_real_span_when_otel_available(fresh_obs):
    obs = fresh_obs
    if not obs._check_otel():
        pytest.skip("opentelemetry not importable in this env")
    with obs.evaluator_span(
        policy_name="my-policy",
        evaluator_type="ZentropiEvaluator",
        host="claude-code",
        trigger="before_response",
    ) as span:
        # Span is non-None; attribute setters reachable.
        assert span is not None
        obs.record_verdict(span, label=1, confidence=0.9, latency_ms=200.0)


def test_increment_verdict_counter_does_not_crash_when_otel_available(fresh_obs):
    obs = fresh_obs
    if not obs._check_otel():
        pytest.skip("opentelemetry not importable in this env")
    obs.increment_verdict_counter("deny")
    # Counter is memoized; second call reuses the same instrument.
    obs.increment_verdict_counter("deny")


def test_record_latency_does_not_crash_when_otel_available(fresh_obs):
    obs = fresh_obs
    if not obs._check_otel():
        pytest.skip("opentelemetry not importable in this env")
    obs.record_latency("ZentropiEvaluator", 50.0)
    obs.record_latency("ZentropiEvaluator", 75.0)


# ---- Structured logging mode -------------------------------------------


def test_structured_logging_off_by_default(fresh_obs, monkeypatch):
    obs = fresh_obs
    monkeypatch.delenv("REFLEXES_LOG_FORMAT", raising=False)
    assert obs.configure_structured_logging_if_requested() is False


def test_structured_logging_on_with_env_var(fresh_obs, monkeypatch):
    obs = fresh_obs
    monkeypatch.setenv("REFLEXES_LOG_FORMAT", "json")
    # Add a handler so there's something for the formatter to attach to.
    handler = logging.StreamHandler()
    logging.getLogger().addHandler(handler)
    try:
        result = obs.configure_structured_logging_if_requested()
        assert result is True
        assert isinstance(handler.formatter, obs._JsonFormatter)
    finally:
        logging.getLogger().removeHandler(handler)


def test_structured_logging_case_insensitive(fresh_obs, monkeypatch):
    obs = fresh_obs
    monkeypatch.setenv("REFLEXES_LOG_FORMAT", "JSON")
    assert obs.configure_structured_logging_if_requested() is True


def test_structured_logging_other_values_off(fresh_obs, monkeypatch):
    obs = fresh_obs
    monkeypatch.setenv("REFLEXES_LOG_FORMAT", "text")
    assert obs.configure_structured_logging_if_requested() is False
    monkeypatch.setenv("REFLEXES_LOG_FORMAT", "")
    assert obs.configure_structured_logging_if_requested() is False


def test_json_formatter_emits_valid_json():
    import json
    from reflexes.observability import _JsonFormatter

    formatter = _JsonFormatter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="x.py", lineno=1,
        msg="hello %s", args=("world",), exc_info=None,
    )
    output = formatter.format(record)
    parsed = json.loads(output)
    assert parsed["level"] == "INFO"
    assert parsed["message"] == "hello world"
    assert parsed["logger"] == "test"


def test_json_formatter_includes_extra_fields():
    import json
    from reflexes.observability import _JsonFormatter

    formatter = _JsonFormatter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="x.py", lineno=1,
        msg="msg", args=(), exc_info=None,
    )
    record.session_id = "sid-123"
    record.policy_name = "negative-instruction-violation"
    output = formatter.format(record)
    parsed = json.loads(output)
    assert parsed["session_id"] == "sid-123"
    assert parsed["policy_name"] == "negative-instruction-violation"


def test_json_formatter_handles_unserializable_extras():
    """Non-JSON-serializable extras are coerced to repr() rather than crashing."""
    import json
    from reflexes.observability import _JsonFormatter

    formatter = _JsonFormatter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="x.py", lineno=1,
        msg="msg", args=(), exc_info=None,
    )
    # A class instance isn't JSON-serializable.
    class _NotSerializable:
        def __repr__(self):
            return "<unserializable>"

    record.weird = _NotSerializable()
    output = formatter.format(record)
    parsed = json.loads(output)
    assert parsed["weird"] == "<unserializable>"
