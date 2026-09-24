import io
import json
import logging

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import StatusCode

from argus import telemetry, tracing
from argus.domain.errors import AgentRunError
from argus.domain.models import StageMetrics


def test_tracing_is_off_without_an_endpoint() -> None:
    assert tracing.build_provider({}) is None
    assert tracing.build_provider({tracing.ENDPOINT_ENV: "  "}) is None


def test_tracing_is_on_with_an_endpoint_and_names_the_service() -> None:
    provider = tracing.build_provider({tracing.ENDPOINT_ENV: "http://127.0.0.1:9"})
    try:
        assert isinstance(provider, TracerProvider)
        assert provider.resource.attributes["service.name"] == "argus"
    finally:
        provider.shutdown()


def test_session_without_an_endpoint_installs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    installed: list[object] = []
    monkeypatch.setattr(tracing.trace, "set_tracer_provider", installed.append)

    with tracing.session({}) as enabled:
        assert enabled is False
    assert installed == []


def test_session_installs_the_provider_and_flushes_it_on_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: list[TracerProvider] = []
    monkeypatch.setattr(tracing.trace, "set_tracer_provider", installed.append)

    with tracing.session({tracing.ENDPOINT_ENV: "http://127.0.0.1:9"}) as enabled:
        assert enabled is True
        (provider,) = installed
        shut: list[bool] = []
        monkeypatch.setattr(provider, "shutdown", lambda: shut.append(True))
    assert shut == [True]


def test_session_redacts_credentials_in_the_logged_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = io.StringIO()
    telemetry.configure(log_format="json", stream=stream)
    monkeypatch.setattr(tracing.trace, "set_tracer_provider", lambda _p: None)

    with tracing.session({tracing.ENDPOINT_ENV: "https://u:ghp_abcdefghijkl@127.0.0.1:9"}):
        pass

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    (event,) = [e for e in events if e["event"] == "tracing_enabled"]
    assert "ghp_abcdefghijkl" not in event["endpoint"]


@pytest.mark.parametrize(
    "secret",
    ["sk-ant-oat01-abcdefghij", "ghp_abcdefghijkl", "ghs_abcdefghijkl", "github_pat_abcdefghij"],
)
def test_redact_removes_credentials(secret: str) -> None:
    assert secret not in tracing.redact(f"token={secret} rest")
    assert "[redacted]" in tracing.redact(secret)


def test_redact_truncates() -> None:
    assert tracing.redact("x" * 50, limit=10) == "x" * 7 + "..."


def test_meta_prefixes_keys_and_drops_none() -> None:
    assert tracing.meta(a=1, b=None, c="x") == {
        "langsmith.metadata.a": 1,
        "langsmith.metadata.c": "x",
    }


def test_meta_redacts_string_values_but_leaves_other_types_alone() -> None:
    attrs = tracing.meta(token="ghp_abcdefghijkl1234", n=3)
    assert "ghp_abcdefghijkl1234" not in attrs["langsmith.metadata.token"]
    assert attrs["langsmith.metadata.n"] == 3


def test_clean_redacts_strings_and_leaves_other_types_alone() -> None:
    attrs = tracing.clean({"token": "ghp_abcdefghijkl1234", "n": 3, "ok": True})
    assert "ghp_abcdefghijkl1234" not in attrs["token"]
    assert "[redacted]" in attrs["token"]
    assert attrs["n"] == 3
    assert attrs["ok"] is True


def test_span_redacts_the_display_name_and_string_attributes(spans) -> None:
    with tracing.span(
        "x",
        "chain",
        display="d ghp_abcdefghijkl1234",
        attributes={"input.value": "ghp_abcdefghijkl1234"},
    ):
        pass

    (s,) = spans.get_finished_spans()
    assert "ghp_abcdefghijkl1234" not in s.attributes["langsmith.trace.name"]
    assert "ghp_abcdefghijkl1234" not in s.attributes["input.value"]


def test_stage_attributes_carry_the_stage_metrics() -> None:
    m = StageMetrics(
        stage="review",
        model="claude-opus-5",
        cost_usd=0.3,
        input_tokens=1,
        output_tokens=2,
        cache_read_input_tokens=3,
        cache_creation_input_tokens=4,
        num_turns=5,
        duration_ms=6,
        subagents_run=3,
        output_rejections=1,
    )

    attrs = tracing.stage_attributes(m)

    assert attrs["gen_ai.request.model"] == "claude-opus-5"
    assert attrs["langsmith.metadata.cost_usd"] == 0.3
    assert attrs["langsmith.metadata.num_turns"] == 5
    assert attrs["langsmith.metadata.subagents_run"] == 3
    assert attrs["langsmith.metadata.output_rejections"] == 1
    assert attrs["langsmith.metadata.cache_read_input_tokens"] == 3


def test_span_marks_errors_redacted_with_their_cost(spans) -> None:
    with pytest.raises(AgentRunError), tracing.span("argus.review", "chain", display="review"):
        raise AgentRunError("error_max_turns", 0.25, detail="ghp_abcdefghijkl leaked")

    (s,) = spans.get_finished_spans()
    assert s.attributes["langsmith.span.kind"] == "chain"
    assert s.attributes["langsmith.trace.name"] == "review"
    assert s.status.status_code == StatusCode.ERROR
    assert "ghp_abcdefghijkl" not in s.status.description
    assert s.status.description.startswith("AgentRunError")
    assert s.attributes["langsmith.metadata.cost_usd"] == 0.25
    assert s.events == ()  # no raw exception event carrying the unredacted message


def test_exporter_warnings_become_structured_log_events(monkeypatch) -> None:
    stream = io.StringIO()
    telemetry.configure(log_format="json", stream=stream)
    monkeypatch.setattr(tracing.trace, "set_tracer_provider", lambda _p: None)

    with tracing.session({tracing.ENDPOINT_ENV: "http://127.0.0.1:9"}):
        logging.getLogger("opentelemetry.exporter.otlp").warning("401 for ghp_abcdefghijkl")

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    (event,) = [e for e in events if e["event"] == "otel_warning"]
    assert event["logger"] == "opentelemetry.exporter.otlp"
    assert "ghp_abcdefghijkl" not in event["message"]
