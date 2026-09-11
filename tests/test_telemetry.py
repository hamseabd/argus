import io
import json

import structlog

from argus import telemetry


def configure_json() -> io.StringIO:
    stream = io.StringIO()
    telemetry.configure(log_format="json", stream=stream)
    return stream


def test_json_format_writes_one_json_object_per_event() -> None:
    stream = configure_json()

    telemetry.get_logger().info("run_start", source="local")

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(lines) == 1
    assert lines[0]["event"] == "run_start"
    assert lines[0]["source"] == "local"
    assert lines[0]["level"] == "info"
    assert "timestamp" in lines[0]


def test_run_context_is_attached_to_every_event() -> None:
    stream = configure_json()

    telemetry.bind_run(run_id="run-1")
    telemetry.get_logger().info("stage_start", stage="review")
    telemetry.bind_run(session_id="sess-9")
    telemetry.get_logger().info("stage_end", stage="review")

    first, second = (json.loads(line) for line in stream.getvalue().splitlines())
    assert first["run_id"] == "run-1"
    assert "session_id" not in first
    assert second["run_id"] == "run-1"
    assert second["session_id"] == "sess-9"


def test_new_run_id_is_unique_and_short() -> None:
    a = telemetry.new_run_id()
    b = telemetry.new_run_id()

    assert a != b
    assert 8 <= len(a) <= 32


def test_auto_format_picks_console_on_a_tty_and_json_otherwise() -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    assert telemetry.resolve_format("auto", Tty()) == "console"
    assert telemetry.resolve_format("auto", io.StringIO()) == "json"
    assert telemetry.resolve_format("json", Tty()) == "json"
    assert telemetry.resolve_format("console", io.StringIO()) == "console"


def test_console_format_is_human_readable() -> None:
    stream = io.StringIO()
    telemetry.configure(log_format="console", stream=stream)

    telemetry.get_logger().info("run_end", cost=0.5)

    out = stream.getvalue()
    assert "run_end" in out
    assert not out.lstrip().startswith("{")


def test_configure_resets_bound_context() -> None:
    stream = configure_json()
    telemetry.bind_run(run_id="stale")
    telemetry.configure(log_format="json", stream=stream)

    telemetry.get_logger().info("x")

    assert "stale" not in stream.getvalue()


def test_debug_events_are_dropped_at_info_and_kept_at_debug() -> None:
    stream = io.StringIO()
    telemetry.configure(log_format="json", stream=stream)
    telemetry.get_logger().debug("cli_stderr", line="x")
    assert stream.getvalue() == ""

    telemetry.configure(log_format="json", stream=stream, level="debug")
    telemetry.get_logger().debug("cli_stderr", line="x")
    assert json.loads(stream.getvalue())["event"] == "cli_stderr"


def test_module_uses_structlog_not_print() -> None:
    assert isinstance(telemetry.get_logger(), structlog.typing.FilteringBoundLogger | object)
