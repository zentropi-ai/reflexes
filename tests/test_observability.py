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


# ---- Diagnostic logging (configure_logging, config-driven) --------------


@pytest.fixture
def fresh_logging(monkeypatch):
    """Reset the one-shot logging guard + the reflexes logger between tests."""
    import reflexes.observability as obs
    monkeypatch.setattr(obs, "_LOGGING_CONFIGURED", False)
    rlog = logging.getLogger("reflexes")
    saved_level, saved_prop = rlog.level, rlog.propagate
    saved_handlers = list(rlog.handlers)
    yield obs
    for h in list(rlog.handlers):
        if h not in saved_handlers:
            rlog.removeHandler(h)
    rlog.setLevel(saved_level)
    rlog.propagate = saved_prop


def _write_cfg(tmp_path, logging_block=""):
    (tmp_path / "reflexes.yaml").write_text(
        logging_block
        + "reflexes:\n  default: []\nevaluators:\n  default:\n    type: zentropi\n"
    )
    return str(tmp_path / "reflexes.yaml")


def test_default_level_is_warning_no_content(fresh_logging, tmp_path):
    """Absent `logging:` block → WARNING. Content is DEBUG-only, so the default
    means user content is never captured."""
    obs = fresh_logging
    obs.configure_logging(_write_cfg(tmp_path), force=True)
    assert logging.getLogger("reflexes").level == logging.WARNING


def test_level_debug_opts_into_full_capture(fresh_logging, tmp_path):
    obs = fresh_logging
    cfg = _write_cfg(tmp_path, "logging:\n  level: debug\n")
    obs.configure_logging(cfg, force=True)
    assert logging.getLogger("reflexes").level == logging.DEBUG


def test_level_off_silences_everything(fresh_logging, tmp_path):
    obs = fresh_logging
    cfg = _write_cfg(tmp_path, "logging:\n  level: off\n")
    obs.configure_logging(cfg, force=True)
    # Above CRITICAL → no record of any level emits.
    assert logging.getLogger("reflexes").level > logging.CRITICAL


def test_garbled_level_fails_safe_to_warning(fresh_logging, tmp_path):
    """An unknown/typo'd level must never resolve to a more-verbose level."""
    obs = fresh_logging
    cfg = _write_cfg(tmp_path, "logging:\n  level: verbose\n")
    obs.configure_logging(cfg, force=True)
    assert logging.getLogger("reflexes").level == logging.WARNING


def test_format_json_installs_json_formatter(fresh_logging, tmp_path):
    obs = fresh_logging
    handler = logging.StreamHandler()
    logging.getLogger().addHandler(handler)
    try:
        cfg = _write_cfg(tmp_path, "logging:\n  format: json\n")
        obs.configure_logging(cfg, force=True)
        assert isinstance(handler.formatter, obs._JsonFormatter)
    finally:
        logging.getLogger().removeHandler(handler)


def test_guard_first_caller_wins_without_force(fresh_logging, tmp_path):
    """configure_from_file (no force) must not clobber a prior forced config."""
    obs = fresh_logging
    obs.configure_logging(_write_cfg(tmp_path, "logging:\n  level: debug\n"), force=True)
    # A later non-forced call with a different level is ignored.
    other = tmp_path / "other.yaml"
    other.write_text("logging:\n  level: off\nreflexes:\n  default: []\n")
    obs.configure_logging(str(other))
    assert logging.getLogger("reflexes").level == logging.DEBUG


def test_host_prefix_in_text_log(fresh_logging, tmp_path):
    """The host tag distinguishes hooks that share one log file (Codex reuses
    the Claude Code hook → same debug.log)."""
    obs = fresh_logging
    logfile = tmp_path / "d.log"
    obs.configure_logging(
        _write_cfg(tmp_path, "logging:\n  level: debug\n"),
        log_file=str(logfile), host="codex", force=True,
    )
    logging.getLogger("reflexes.probe").warning("hi")
    for h in logging.getLogger("reflexes").handlers:
        h.flush()
    assert "[codex]" in logfile.read_text()


def test_host_field_in_json_log(fresh_logging, tmp_path):
    import json as _json
    obs = fresh_logging
    logfile = tmp_path / "d.log"
    obs.configure_logging(
        _write_cfg(tmp_path, "logging:\n  level: debug\n  format: json\n"),
        log_file=str(logfile), host="hermes", force=True,
    )
    logging.getLogger("reflexes.probe").warning("hi")
    for h in logging.getLogger("reflexes").handlers:
        h.flush()
    record = _json.loads(logfile.read_text().strip().splitlines()[0])
    assert record["host"] == "hermes"


def test_stream_handler_emits_at_configured_level(fresh_logging, tmp_path):
    """A subprocess host without a log file (Hermes) passes stream=stderr so its
    configured level actually surfaces — Python's last-resort handler is
    WARNING-only, so a debug line would otherwise vanish."""
    import io
    obs = fresh_logging
    buf = io.StringIO()
    obs.configure_logging(
        _write_cfg(tmp_path, "logging:\n  level: debug\n"),
        stream=buf, host="hermes", force=True,
    )
    logging.getLogger("reflexes.probe").debug("deep detail")
    for h in logging.getLogger("reflexes").handlers:
        h.flush()
    out = buf.getvalue()
    assert "deep detail" in out and "[hermes]" in out and "DEBUG" in out


def test_off_creates_no_log_file(fresh_logging, tmp_path):
    """`off` with a log_file target must not create the file."""
    obs = fresh_logging
    log_file = tmp_path / "debug.log"
    cfg = _write_cfg(tmp_path, "logging:\n  level: off\n")
    obs.configure_logging(cfg, log_file=str(log_file), force=True)
    assert not log_file.exists()


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
