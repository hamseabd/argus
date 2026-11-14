import io

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from argus import telemetry, tracing

_SPANS = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(_SPANS))
trace.set_tracer_provider(_provider)  # once per process; OTel refuses a second global provider


@pytest.fixture(autouse=True)
def isolated_logging() -> None:
    """Every test starts with fresh, silent JSON logging and no bound run context."""
    telemetry.configure(log_format="json", stream=io.StringIO())


@pytest.fixture(autouse=True)
def no_installed_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts as a fresh process would, with no provider from tracing.session()."""
    monkeypatch.setattr(tracing, "_installed", None)


@pytest.fixture
def spans() -> InMemorySpanExporter:
    """Every span the code under test finished, starting empty."""
    _SPANS.clear()
    return _SPANS


@pytest.fixture(autouse=True)
def plain_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI output without color or terminal-dependent wrapping.

    Typer renders errors through rich, which on a color terminal splits words
    like `--diff` into separately styled runs; assertions on the text would
    then depend on the runner's TERM and FORCE_COLOR.
    """
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
