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

import logging
import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode
from opentelemetry.util.types import AttributeValue

from argus import __version__
from argus.domain.models import StageMetrics
from argus.telemetry import get_logger

ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
KIND = "langsmith.span.kind"
NAME = "langsmith.trace.name"
METADATA = "langsmith.metadata."
MAX_ATTRIBUTE_CHARS = 8192
ERROR_CHARS = 300

_CREDENTIAL = re.compile(r"(sk-ant-|gh[pousr]_|github_pat_)[A-Za-z0-9_\-]{8,}")
"""Review logs and traces leave the machine; nothing token-shaped goes into one.

Both credentials the review step holds are covered: the Claude subscription
token it runs on, and the app installation token it posts with.
"""


def tracer() -> trace.Tracer:
    return trace.get_tracer("argus", __version__)


def redact(text: str, limit: int = MAX_ATTRIBUTE_CHARS) -> str:
    cleaned = _CREDENTIAL.sub("[redacted]", text)
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3] + "..."


def meta(**values: object) -> dict[str, AttributeValue]:
    """Attributes LangSmith would otherwise drop, under its metadata prefix; None is skipped.

    Every string value passes through redact() so no credential-shaped text
    reaches a span by way of metadata; other types are passed through as-is.
    """
    return {
        f"{METADATA}{k}": redact(v) if isinstance(v, str) else v  # type: ignore[misc]
        for k, v in values.items()
        if v is not None
    }


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
    """ERROR status with a redacted one-line reason, and what the failure cost when it was paid."""
    span.set_status(Status(StatusCode.ERROR, redact(f"{type(exc).__name__}: {exc}", ERROR_CHARS)))
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
        **{k: redact(v) if isinstance(v, str) else v for k, v in (attributes or {}).items()},
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


def build_provider(environ: Mapping[str, str] = os.environ) -> TracerProvider | None:
    if not environ.get(ENDPOINT_ENV, "").strip():
        return None
    resource = Resource.create({"service.name": "argus", "service.version": __version__})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    return provider


@contextmanager
def session(environ: Mapping[str, str] = os.environ) -> Iterator[bool]:
    """Install the provider for the block and flush it on the way out; yields whether tracing is on.

    A short-lived CLI run would otherwise exit before the batch processor sends.
    """
    provider = build_provider(environ)
    if provider is None:
        yield False
        return
    trace.set_tracer_provider(provider)
    bridge = _StructlogBridge()
    otel_logger = logging.getLogger("opentelemetry")
    otel_logger.addHandler(bridge)
    otel_logger.propagate = False
    get_logger().info("tracing_enabled", endpoint=redact(environ[ENDPOINT_ENV]))
    try:
        yield True
    finally:
        provider.shutdown()
        otel_logger.removeHandler(bridge)
        otel_logger.propagate = True


class _StructlogBridge(logging.Handler):
    """OTel reports export failures through stdlib logging; keep them structured and redacted."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        get_logger().warning(
            "otel_warning", logger=record.name, message=redact(record.getMessage(), ERROR_CHARS)
        )
