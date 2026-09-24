"""OpenTelemetry traces for a review run.

Argus builds its own spans rather than exporting Claude Code's native ones:
the native spans carry the account's email and ids, and LangSmith drops
every attribute it does not map, so native tool spans arrive nameless and
token counts read zero. Spans here use the gen_ai conventions plus
LangSmith's own keys; anything else goes under langsmith.metadata.

Tracing is on only when OTEL_EXPORTER_OTLP_ENDPOINT is set. The exporter
reads the standard OTEL_EXPORTER_OTLP_* variables itself. Off, the OTel API
is a no-op and nothing here changes a run. This module does not import the
Agent SDK.
"""

import json
import logging
import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk import trace as sdk_trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import Span, Status, StatusCode
from opentelemetry.util.types import AttributeValue

from argus import __version__
from argus.domain.models import StageMetrics
from argus.telemetry import get_logger

ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
TRACER_NAME = "argus"
KIND = "langsmith.span.kind"
NAME = "langsmith.trace.name"
METADATA = "langsmith.metadata."
MAX_ATTRIBUTE_CHARS = 8192
ERROR_CHARS = 300

_CREDENTIAL = re.compile(r"(sk-ant-|gh[pousr]_|github_pat_|lsv2_)[A-Za-z0-9_\-]{8,}")
"""Review logs and traces leave the machine; nothing token-shaped goes into one.

All three credentials the review step holds are covered: the Claude
subscription token it runs on, the app installation token it posts with,
and the LangSmith key it traces with.
"""


def tracer() -> trace.Tracer:
    return trace.get_tracer(TRACER_NAME, __version__)


def redact(text: str, limit: int = MAX_ATTRIBUTE_CHARS) -> str:
    cleaned = _CREDENTIAL.sub("[redacted]", text)
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3] + "..."


def summarize(value: object, limit: int = 200) -> str:
    """A tool input as one redacted, bounded line: sorted JSON, or repr() when JSON cannot hold it.

    Shared by the audit log and the trace so both show a call the same way.
    """
    try:
        text = json.dumps(value, sort_keys=True)
    except (TypeError, ValueError):
        text = repr(value)
    return redact(text, limit)


def clean(attributes: Mapping[str, AttributeValue]) -> dict[str, AttributeValue]:
    """A copy of attributes with every str value passed through redact(); other types untouched.

    The one place that decides whether a value is credential-shaped, so every
    caller that builds attributes for a span shares the same rule.
    """
    return {k: redact(v) if isinstance(v, str) else v for k, v in attributes.items()}


def meta(**values: object) -> dict[str, AttributeValue]:
    """Attributes LangSmith would otherwise drop, under its metadata prefix; None is skipped.

    Every string value passes through redact() so no credential-shaped text
    reaches a span by way of metadata; other types are passed through as-is.
    """
    present: dict[str, AttributeValue] = {k: v for k, v in values.items() if v is not None}  # type: ignore[misc]
    return {f"{METADATA}{k}": v for k, v in clean(present).items()}


def stage_attributes(metrics: StageMetrics) -> dict[str, AttributeValue]:
    """A stage's totals. Token totals stay in metadata so LangSmith does not add them twice."""
    return {
        "gen_ai.request.model": metrics.model,
        **meta(
            stage=metrics.stage,
            cost_usd=metrics.cost_usd,
            num_turns=metrics.num_turns,
            duration_ms=metrics.duration_ms,
            input_tokens=metrics.input_tokens,
            output_tokens=metrics.output_tokens,
            cache_read_input_tokens=metrics.cache_read_input_tokens,
            cache_creation_input_tokens=metrics.cache_creation_input_tokens,
            subagents_run=metrics.subagents_run,
            output_rejections=metrics.output_rejections,
        ),
    }


def record_error(span: Span, exc: BaseException) -> None:
    """ERROR status with a redacted one-line reason, and what the failure cost when it was paid.

    LangSmith ignores the OTel status and marks a run failed only from an
    exception event, so one is added too: type and redacted message, never
    the stack trace.
    """
    reason = redact(f"{type(exc).__name__}: {exc}", ERROR_CHARS)
    span.set_status(Status(StatusCode.ERROR, reason))
    span.add_event(
        "exception",
        {"exception.type": type(exc).__name__, "exception.message": redact(str(exc), ERROR_CHARS)},
    )
    cost = getattr(exc, "cost_usd", None)
    if isinstance(cost, int | float):
        span.set_attributes(meta(cost_usd=float(cost)))


@contextmanager
def span(
    name: str,
    kind: str,
    *,
    display: str | None = None,
    attributes: Mapping[str, AttributeValue] | None = None,
) -> Iterator[Span]:
    """The current span for a block. Errors are recorded redacted, never as raw exception events."""
    attrs: dict[str, AttributeValue] = {
        KIND: kind,
        **clean(attributes or {}),
    }
    if display is not None:
        attrs[NAME] = redact(display)
    with tracer().start_as_current_span(
        name, attributes=attrs, record_exception=False, set_status_on_exception=False
    ) as current:
        try:
            yield current
        except BaseException as exc:
            record_error(current, exc)
            raise


_installed: TracerProvider | None = None
"""The provider session() installed for this process, if any."""


def build_provider(
    environ: Mapping[str, str] = os.environ, *, exporter: SpanExporter | None = None
) -> TracerProvider | None:
    """The provider for a traced run, or None when no endpoint is set.

    exporter defaults to OTLP over HTTP, configured from the OTEL_* variables.
    """
    if not environ.get(ENDPOINT_ENV, "").strip():
        return None
    resource = Resource.create({"service.name": "argus", "service.version": __version__})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(_ArgusOnly(BatchSpanProcessor(exporter or OTLPSpanExporter())))
    return provider


@contextmanager
def session(environ: Mapping[str, str] = os.environ) -> Iterator[bool]:
    """Trace the block and flush on the way out; yields whether tracing is on.

    A short-lived CLI run would otherwise exit before the batch processor sends.
    OTel accepts one global provider per process, so the first session installs
    it and later ones reuse it; the provider shuts itself down at exit.
    A configuration the exporter rejects (a malformed OTEL_* variable) turns
    tracing off with a warning; it never stops the review.
    """
    global _installed
    provider = _installed
    if provider is None:
        try:
            provider = build_provider(environ)
        except Exception as exc:
            # Yield outside the handler, or a review failure would chain to this error.
            get_logger().warning("tracing_disabled", error=redact(str(exc), ERROR_CHARS))
        if provider is None:
            yield False
            return
        trace.set_tracer_provider(provider)
        _installed = provider
    bridge = _StructlogBridge()
    otel_logger = logging.getLogger("opentelemetry")
    otel_logger.addHandler(bridge)
    otel_logger.propagate = False
    get_logger().info("tracing_enabled", endpoint=redact(environ.get(ENDPOINT_ENV, "")))
    try:
        yield True
    finally:
        provider.force_flush()
        otel_logger.removeHandler(bridge)
        otel_logger.propagate = True


class _ArgusOnly(SpanProcessor):
    """Export only the spans Argus builds.

    Libraries in the process instrument themselves once a provider is
    installed (the mcp package emits initialize, tools/list, and its own tool
    spans), which would land in the review's trace unredacted.
    """

    def __init__(self, inner: SpanProcessor) -> None:
        self._inner = inner

    def on_start(self, span: sdk_trace.Span, parent_context: Context | None = None) -> None:
        self._inner.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        scope = span.instrumentation_scope
        if scope is not None and scope.name == TRACER_NAME:
            self._inner.on_end(span)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


class _StructlogBridge(logging.Handler):
    """OTel reports export failures through stdlib logging; keep them structured and redacted."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        get_logger().warning(
            "otel_warning", logger=record.name, message=redact(record.getMessage(), ERROR_CHARS)
        )
