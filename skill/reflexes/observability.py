"""OpenTelemetry observability scaffolding.

Optional dependency: ``pip install reflexes[otel]`` installs
``opentelemetry-api`` + ``opentelemetry-sdk``. Without the extra,
every public function in this module is a no-op — zero runtime cost
for users who don't use it.

The SDK plugs into popular observability frameworks. OpenTelemetry
is the vendor-neutral choice (Datadog / Honeycomb / Splunk / New Relic
all consume OTLP). The instrumented surface:

- Spans: ``reflexes.evaluate`` per policy evaluation, with attributes
  policy.name, evaluator.type, verdict.label, verdict.confidence,
  verdict.latency_ms, host, trigger.
- Counter: ``reflexes.verdicts`` labeled by action (deny / flag / log
  / error / no_match).
- Histogram: ``reflexes.latency`` labeled by evaluator.

Structured-logging mode is a parallel feature: setting
``REFLEXES_LOG_FORMAT=json`` switches the existing loggers to a
JSON-emitting formatter so log-based observability stacks (Loki,
Cloudwatch, etc.) can parse without OTLP setup. Lives in this module
for code locality.
"""
from __future__ import annotations

import json as _json
import logging as _logging
import os
from contextlib import contextmanager


# ---- OTel availability gate ---------------------------------------------

_OTEL_AVAILABLE: bool | None = None


def _check_otel() -> bool:
    """Return True iff opentelemetry-api is importable.

    Memoized: import attempt happens once per process. False is cached
    so subsequent calls don't re-pay the import cost.
    """
    global _OTEL_AVAILABLE
    if _OTEL_AVAILABLE is None:
        try:
            import opentelemetry  # noqa: F401
            _OTEL_AVAILABLE = True
        except ImportError:
            _OTEL_AVAILABLE = False
    return _OTEL_AVAILABLE


# ---- Lazy instrument cache ----------------------------------------------

_meter = None
_tracer = None
_verdicts_counter = None
_latency_histogram = None


def _get_tracer():
    global _tracer
    if _tracer is None:
        from opentelemetry import trace
        _tracer = trace.get_tracer("reflexes")
    return _tracer


def _get_meter():
    global _meter
    if _meter is None:
        from opentelemetry import metrics
        _meter = metrics.get_meter("reflexes")
    return _meter


def _get_verdicts_counter():
    global _verdicts_counter
    if _verdicts_counter is None:
        _verdicts_counter = _get_meter().create_counter(
            "reflexes.verdicts",
            description="Reflex evaluations by action taken",
        )
    return _verdicts_counter


def _get_latency_histogram():
    global _latency_histogram
    if _latency_histogram is None:
        _latency_histogram = _get_meter().create_histogram(
            "reflexes.latency",
            unit="ms",
            description="Per-evaluation latency by evaluator type",
        )
    return _latency_histogram


# ---- Public API ---------------------------------------------------------


@contextmanager
def evaluator_span(
    *,
    policy_name: str,
    evaluator_type: str,
    host: str | None = None,
    trigger: str | None = None,
):
    """Context manager wrapping an evaluator call in a `reflexes.evaluate` span.

    No-op when OTel is unavailable. Attributes set on entry; record_verdict()
    or record_evaluator_error() should be called inside the block to
    annotate the outcome before the span closes.
    """
    if not _check_otel():
        yield None
        return
    span_cm = _get_tracer().start_as_current_span("reflexes.evaluate")
    with span_cm as span:
        span.set_attribute("policy.name", policy_name)
        span.set_attribute("evaluator.type", evaluator_type)
        if host:
            span.set_attribute("host", host)
        if trigger:
            span.set_attribute("trigger", trigger)
        yield span


def record_verdict(span, *, label: int, confidence: float, latency_ms: float | None = None) -> None:
    """Annotate the active evaluator span with verdict attributes."""
    if span is None:
        return
    span.set_attribute("verdict.label", label)
    span.set_attribute("verdict.confidence", confidence)
    if latency_ms is not None:
        span.set_attribute("verdict.latency_ms", latency_ms)


def record_evaluator_error(span, *, error_type: str) -> None:
    """Annotate the active span with an error_type attribute."""
    if span is None:
        return
    span.set_attribute("evaluator.error", error_type)


def increment_verdict_counter(action: str) -> None:
    """Increment ``reflexes.verdicts`` counter labeled by action.

    ``action`` is one of: ``deny`` / ``flag`` / ``log`` / ``error`` /
    ``no_match``. Each evaluation should result in exactly one counter
    increment.
    """
    if not _check_otel():
        return
    try:
        _get_verdicts_counter().add(1, {"action": action})
    except Exception:
        pass  # observability must never crash the host


def record_latency(evaluator_type: str, latency_ms: float) -> None:
    """Record latency on the ``reflexes.latency`` histogram."""
    if not _check_otel():
        return
    try:
        _get_latency_histogram().record(latency_ms, {"evaluator": evaluator_type})
    except Exception:
        pass


# ---- Structured logging mode --------------------------------------------


class _JsonFormatter(_logging.Formatter):
    """Minimal JSON log formatter for log-based observability stacks.

    Emits one JSON object per line with fields name, level, message,
    timestamp. Extra fields can be attached to log records via
    ``logger.info(msg, extra={"key": val})``.
    """

    _STD_RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname",
        "filename", "module", "exc_info", "exc_text", "stack_info",
        "lineno", "funcName", "created", "msecs", "relativeCreated",
        "thread", "threadName", "processName", "process", "message",
        "asctime", "taskName",
    }

    def format(self, record: _logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k in self._STD_RESERVED or k.startswith("_"):
                continue
            try:
                _json.dumps(v)
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = repr(v)
        return _json.dumps(payload)


def configure_structured_logging_if_requested() -> bool:
    """If ``REFLEXES_LOG_FORMAT=json``, install JSON formatter on root.

    Idempotent: re-running replaces the active formatter on existing
    handlers rather than stacking. Returns True if structured mode is
    active. Called from integrations at startup so they pick up the
    env-var directive without each one re-implementing the swap.
    """
    if os.environ.get("REFLEXES_LOG_FORMAT", "").lower() != "json":
        return False
    formatter = _JsonFormatter()
    root = _logging.getLogger()
    for handler in root.handlers:
        handler.setFormatter(formatter)
    return True
