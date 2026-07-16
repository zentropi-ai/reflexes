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

Diagnostic logging is a parallel feature, configured entirely from the
``logging:`` block in reflexes.yaml (see ``configure_logging``) — no env
vars. The default level is WARNING (enforcement problems + ``log``-action
hits, never message content, which is DEBUG-only); ``off`` silences
everything; ``format: json`` swaps in a JSON formatter so log-based
observability stacks (Loki, Cloudwatch, etc.) can parse without OTLP setup.
Lives in this module for code locality.
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
    timestamp, and (when set) the originating host. Extra fields can be
    attached to log records via ``logger.info(msg, extra={"key": val})``.
    """

    _STD_RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname",
        "filename", "module", "exc_info", "exc_text", "stack_info",
        "lineno", "funcName", "created", "msecs", "relativeCreated",
        "thread", "threadName", "processName", "process", "message",
        "asctime", "taskName",
    }

    def __init__(self, host: str | None = None):
        super().__init__()
        self._host = host

    def format(self, record: _logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if self._host:
            payload["host"] = self._host
        for k, v in record.__dict__.items():
            if k in self._STD_RESERVED or k.startswith("_"):
                continue
            try:
                _json.dumps(v)
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = repr(v)
        return _json.dumps(payload)


_LEVEL_MAP = {
    "error": _logging.ERROR,
    "warning": _logging.WARNING,
    "info": _logging.INFO,
    "debug": _logging.DEBUG,
}
_SILENT = _logging.CRITICAL + 1  # above every real level → nothing emits

_LOGGING_CONFIGURED = False


def _open_secure_logfile(path: str):
    """Return a FileHandler whose backing file is owner-only (0o600), created
    atomically at that mode before any write. Returns None if it can't open."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)
        try:
            os.chmod(path, 0o600)  # tighten if it pre-existed wider
        except OSError:
            pass
        return _logging.FileHandler(path)
    except OSError:
        return None


def configure_logging(
    config_file="reflexes.yaml",
    *,
    log_file: str | None = None,
    stream=None,
    host: str | None = None,
    force: bool = False,
):
    """Configure Reflexes diagnostic logging from reflexes.yaml's ``logging:``
    block — the single, host-agnostic logging entry point.

    - level ``off``/``none`` → nothing is emitted (and no log file is created).
    - Otherwise the ``reflexes`` logger tree is set to the level. Message
      CONTENT is only ever logged at DEBUG, so the WARNING default surfaces
      enforcement problems (fail-open, evaluator errors) and ``log``-action
      hits without capturing any user content.
    - ``log_file`` (Claude Code / subprocess hooks): also attach an owner-only
      file handler at the level. ``stream`` (e.g. the Hermes shell hook passing
      ``sys.stderr``): attach a stream handler instead — needed because Python's
      last-resort handler only emits WARNING+, so without one a subprocess's
      ``debug`` level would never surface. In-process SDK hosts pass neither and
      inherit the app's handlers; emission is still gated by the logger level.
    - ``host`` prefixes every record with the originating host (e.g. ``[codex]``
      in text, a ``host`` field in JSON). Multiple subprocess hooks on one
      machine share a log file — Codex reuses the Claude Code hook and writes to
      the same ``debug.log`` — so the tag is what tells their lines apart.
    - format ``json`` swaps in the structured formatter for log-aggregation.

    Guarded: the first caller wins unless a later caller passes ``force=True``.
    The Claude Code hook forces (it owns the file); ``configure_from_file``
    (which every host funnels through) does not — so the file-owning path is
    never clobbered by the generic one.
    """
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED and not force:
        return None
    from reflexes.config import read_log_settings
    level_str, fmt = read_log_settings(config_file)
    rlog = _logging.getLogger("reflexes")

    # Idempotent re-config: drop any file handler we attached before.
    for h in list(rlog.handlers):
        if getattr(h, "_reflexes_owned", False):
            rlog.removeHandler(h)

    if level_str in ("off", "none"):
        rlog.setLevel(_SILENT)  # suppresses every record regardless of handlers
        _LOGGING_CONFIGURED = True
        return level_str

    level = _LEVEL_MAP.get(level_str, _logging.WARNING)
    rlog.setLevel(level)
    host_tag = f"[{host}] " if host else ""
    formatter = _JsonFormatter(host=host) if fmt == "json" else _logging.Formatter(
        f"%(asctime)s {host_tag}%(name)s %(levelname)s: %(message)s"
    )
    handler = None
    if log_file:
        handler = _open_secure_logfile(log_file)
    elif stream is not None:
        handler = _logging.StreamHandler(stream)
    if handler is not None:
        handler.setLevel(level)
        handler.setFormatter(formatter)
        handler._reflexes_owned = True
        rlog.addHandler(handler)
        rlog.propagate = False  # our handler is the sink; don't double to root
    elif fmt == "json":
        # In-process host: restyle whatever handlers the app already installed.
        for h in _logging.getLogger().handlers:
            h.setFormatter(formatter)
    _LOGGING_CONFIGURED = True
    return level_str
