import io
import json

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock, ThinkingBlock
from opentelemetry.trace import StatusCode

from argus import telemetry
from argus.agent import trace as trace_module
from argus.agent.trace import TraceRecorder
from argus.tracing import span


class Clock:
    def __init__(self) -> None:
        self.now = 1_000

    def __call__(self) -> int:
        self.now += 1_000
        return self.now


def pre(tool: str, tu: str, agent_id: str | None = None, **tool_input) -> dict:
    data = {"tool_name": tool, "tool_use_id": tu, "tool_input": tool_input}
    if agent_id:
        data |= {"agent_id": agent_id, "agent_type": "correctness"}
    return data


def turn(mid: str, parent: str | None = None, text: str = "t", **usage) -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text)],
        model="claude-sonnet-5" if parent else "claude-opus-5",
        parent_tool_use_id=parent,
        message_id=mid,
        usage=usage or {"input_tokens": 2, "output_tokens": 10, "cache_read_input_tokens": 100},
    )


def copy_of(mid: str, content: list, parent: str | None = None, **usage) -> AssistantMessage:
    """One streamed copy of a turn: same message_id, an explicit block list."""
    return AssistantMessage(
        content=content,
        model="claude-sonnet-5" if parent else "claude-opus-5",
        parent_tool_use_id=parent,
        message_id=mid,
        usage=usage or None,
    )


EMAIL = "dev@example.com"
CLAUDE = "sk-ant-oat01-abcdefghijklmnop"
LANGSMITH = "lsv2_pt_0123456789abcdef_0123456789"


def record_a_delegation(content: bool = False):
    with span("argus.review", "chain") as stage:
        rec = TraceRecorder(content=content, clock=Clock())
        rec.on_assistant(turn("m1", text="delegating"))
        rec.on_tool_start(pre("Agent", "tu-a", subagent_type="correctness", prompt="review it"))
        rec.on_subagent_start({"agent_id": "ag-1", "agent_type": "correctness"})
        rec.on_assistant(turn("m2", parent="tu-a"))
        rec.on_tool_start(pre("Grep", "tu-g", "ag-1", pattern="ghp_abcdefghijkl1234"))
        rec.on_tool_end(pre("Grep", "tu-g", "ag-1"))
        rec.on_tool_start(
            pre("Read", "tu-r", "ag-1", file_path="nope.py", note=f"{EMAIL} {CLAUDE} {LANGSMITH}")
        )
        rec.on_tool_end(pre("Read", "tu-r", "ag-1"), error="File does not exist")
        rec.on_tool_end(pre("Agent", "tu-a"))
        rec.on_assistant(turn("m3"))
        rec.on_assistant(turn("m3"))  # the stream repeats a message per content block
        rec.close()
    return stage


def named(finished, name):
    return [s for s in finished if s.name == name]


def test_the_subagent_hangs_under_the_stage_and_owns_its_turns_and_tools(spans) -> None:
    record_a_delegation()
    finished = spans.get_finished_spans()
    (stage,) = named(finished, "argus.review")
    (agent,) = named(finished, "correctness")
    (grep,) = named(finished, "Grep")
    (read,) = named(finished, "Read")
    llms = named(finished, "llm")

    assert agent.parent.span_id == stage.context.span_id
    assert agent.attributes["langsmith.span.kind"] == "chain"
    assert agent.attributes["langsmith.metadata.agent_id"] == "ag-1"
    assert grep.parent.span_id == agent.context.span_id
    assert read.parent.span_id == agent.context.span_id
    assert len(llms) == 3  # m1, m2, m3 once
    parents = sorted(s.parent.span_id == agent.context.span_id for s in llms)
    assert parents == [False, False, True]


def test_llm_spans_carry_model_and_tokens(spans) -> None:
    record_a_delegation()
    sub = next(
        s
        for s in named(spans.get_finished_spans(), "llm")
        if s.attributes["gen_ai.request.model"] == "claude-sonnet-5"
    )

    assert sub.attributes["langsmith.span.kind"] == "llm"
    assert sub.attributes["gen_ai.system"] == "anthropic"
    assert sub.attributes["gen_ai.usage.input_tokens"] == 102
    assert sub.attributes["gen_ai.usage.output_tokens"] == 10
    assert sub.attributes["gen_ai.usage.total_tokens"] == 112
    assert sub.attributes["langsmith.metadata.cache_read_input_tokens"] == 100
    assert sub.end_time > sub.start_time


def test_tool_spans_are_named_and_redacted_and_failures_marked(spans) -> None:
    record_a_delegation()
    finished = spans.get_finished_spans()
    (grep,) = named(finished, "Grep")
    (read,) = named(finished, "Read")

    assert grep.attributes["langsmith.span.kind"] == "tool"
    assert grep.attributes["gen_ai.tool.name"] == "Grep"
    assert grep.attributes["gen_ai.tool.call.id"] == "tu-g"
    assert "ghp_abcdefghijkl1234" not in grep.attributes["input.value"]
    assert "[redacted]" in grep.attributes["input.value"]
    assert read.status.status_code == StatusCode.ERROR
    assert "File does not exist" in read.status.description


def test_no_identity_and_no_content_by_default(spans) -> None:
    record_a_delegation()
    for s in spans.get_finished_spans():
        assert not any(k.startswith(("user.", "organization.")) for k in s.attributes)
        assert "output.value" not in s.attributes


@pytest.mark.parametrize("content", [False, True])
def test_no_span_attribute_carries_a_credential_or_an_identity_key(spans, content) -> None:
    """Emails are not redacted by design, so the guard is that no identity key exists at all."""
    record_a_delegation(content=content)
    finished = spans.get_finished_spans()
    assert finished
    for s in finished:
        assert not any(k.startswith(("user.", "organization.")) for k in s.attributes)
        for value in s.attributes.values():
            text = str(value)
            assert not any(t in text for t in ("ghp_", "sk-ant-", "lsv2_")), (s.name, text)


def test_content_mode_adds_assistant_text(spans) -> None:
    record_a_delegation(content=True)
    texts = {s.attributes.get("output.value") for s in named(spans.get_finished_spans(), "llm")}

    assert "delegating" in texts


def test_a_denied_call_ends_once_whichever_hook_runs_first(spans) -> None:
    with span("argus.review", "chain"):
        rec = TraceRecorder(clock=Clock())
        rec.on_tool_denied(pre("Bash", "tu-1", command="rm -rf /"), "Bash is not available")
        rec.on_tool_start(pre("Bash", "tu-1", command="rm -rf /"))  # observer ran second
        rec.on_tool_start(pre("Read", "tu-2", file_path="a.py"))  # observer ran first
        rec.on_tool_denied(pre("Read", "tu-2", file_path="a.py"), "read budget spent")
        rec.close()

    bash = named(spans.get_finished_spans(), "Bash")
    read = named(spans.get_finished_spans(), "Read")
    assert len(bash) == 1 and len(read) == 1
    assert bash[0].attributes["langsmith.metadata.denied"] is True
    assert bash[0].attributes["langsmith.metadata.denial"] == "Bash is not available"
    assert read[0].attributes["langsmith.metadata.denied"] is True


def test_close_ends_what_the_query_left_open_as_errors(spans) -> None:
    with span("argus.review", "chain"):
        rec = TraceRecorder(clock=Clock())
        rec.on_tool_start(pre("Agent", "tu-a", subagent_type="security"))
        rec.on_tool_start(pre("Read", "tu-r", file_path="a.py"))
        rec.close()
        rec.close()  # idempotent

    finished = spans.get_finished_spans()
    for name in ("security", "Read"):
        (s,) = named(finished, name)
        assert s.status.status_code == StatusCode.ERROR


def test_a_rerun_specialist_claims_the_next_delegation_of_its_type(spans) -> None:
    with span("argus.review", "chain"):
        rec = TraceRecorder(clock=Clock())
        rec.on_tool_start(pre("Agent", "tu-1", subagent_type="quality"))
        rec.on_subagent_start({"agent_id": "q-1", "agent_type": "quality"})
        rec.on_tool_end(pre("Agent", "tu-1"))
        rec.on_tool_start(pre("Agent", "tu-2", subagent_type="quality"))
        rec.on_subagent_start({"agent_id": "q-2", "agent_type": "quality"})
        rec.on_tool_start(
            {
                "tool_name": "Read",
                "tool_use_id": "r",
                "tool_input": {},
                "agent_id": "q-2",
                "agent_type": "quality",
            }
        )
        rec.on_tool_end({"tool_name": "Read", "tool_use_id": "r", "agent_id": "q-2"})
        rec.on_tool_end(pre("Agent", "tu-2"))
        rec.close()

    finished = spans.get_finished_spans()
    second = next(
        s for s in named(finished, "quality") if s.attributes["gen_ai.tool.call.id"] == "tu-2"
    )
    (read,) = named(finished, "Read")
    assert read.parent.span_id == second.context.span_id


def test_a_non_text_copy_then_a_text_copy_keep_one_span_with_the_text(spans) -> None:
    with span("argus.review", "chain"):
        rec = TraceRecorder(content=True, clock=Clock())
        rec.on_assistant(
            copy_of(
                "msg-1",
                [ThinkingBlock(thinking="hmm", signature="sig")],
                input_tokens=2,
                output_tokens=10,
                cache_read_input_tokens=100,
            )
        )
        rec.on_assistant(
            copy_of("msg-1", [TextBlock("answer")], input_tokens=999, output_tokens=999)
        )
        rec.close()

    (llm,) = named(spans.get_finished_spans(), "llm")
    assert llm.attributes["output.value"] == "answer"
    assert llm.attributes["gen_ai.usage.input_tokens"] == 102  # from the first copy only
    assert llm.attributes["gen_ai.usage.output_tokens"] == 10


def test_text_from_every_copy_of_a_message_is_kept(spans) -> None:
    with span("argus.review", "chain"):
        rec = TraceRecorder(content=True, clock=Clock())
        rec.on_assistant(copy_of("msg-2", [TextBlock("a")], input_tokens=1, output_tokens=1))
        rec.on_assistant(copy_of("msg-2", [TextBlock("b")]))
        rec.close()

    (llm,) = named(spans.get_finished_spans(), "llm")
    assert "a" in llm.attributes["output.value"]
    assert "b" in llm.attributes["output.value"]


def test_close_flushes_a_pending_turn_with_no_unended_span(spans) -> None:
    with span("argus.review", "chain"):
        rec = TraceRecorder(content=True, clock=Clock())
        rec.on_assistant(turn("m1", text="unflushed"))
        assert not named(spans.get_finished_spans(), "llm")  # still open
        rec.close()

    (llm,) = named(spans.get_finished_spans(), "llm")
    assert llm.attributes["output.value"] == "unflushed"
    assert llm.status.status_code != StatusCode.ERROR


def test_tool_hooks_between_copies_of_one_message_keep_one_llm_span(spans) -> None:
    """One message with several tool_use blocks streams a copy per block, hooks in between."""
    with span("argus.review", "chain"):
        rec = TraceRecorder(content=True, clock=Clock())
        rec.on_tool_start(pre("Agent", "tu-a", subagent_type="correctness"))
        rec.on_subagent_start({"agent_id": "ag-1", "agent_type": "correctness"})
        rec.on_assistant(copy_of("m2", [TextBlock("first")], parent="tu-a", input_tokens=5631))
        rec.on_tool_start(pre("Read", "tu-r", "ag-1", file_path="a.py"))
        rec.on_tool_end(pre("Read", "tu-r", "ag-1"))
        rec.on_assistant(copy_of("m2", [TextBlock("second")], parent="tu-a", input_tokens=5631))
        rec.on_tool_start(pre("Grep", "tu-g", "ag-1", pattern="x"))
        rec.on_tool_end(pre("Grep", "tu-g", "ag-1"))
        rec.on_assistant(copy_of("m2", [TextBlock("third")], parent="tu-a", input_tokens=5631))
        rec.close()

    (llm,) = named(spans.get_finished_spans(), "llm")
    assert llm.attributes["gen_ai.usage.input_tokens"] == 5631
    assert llm.attributes["output.value"] == "first\nsecond\nthird"


def test_a_late_copy_of_an_already_flushed_message_is_dropped(spans) -> None:
    with span("argus.review", "chain"):
        rec = TraceRecorder(content=True, clock=Clock())
        rec.on_assistant(copy_of("m1", [TextBlock("one")], input_tokens=3))
        rec.on_assistant(copy_of("m2", [TextBlock("two")], input_tokens=4))
        rec.on_assistant(copy_of("m1", [TextBlock("stale")], input_tokens=3))
        rec.close()

    llms = named(spans.get_finished_spans(), "llm")
    assert len(llms) == 2
    assert sum(s.attributes["gen_ai.usage.input_tokens"] for s in llms) == 7
    assert all("stale" not in s.attributes["output.value"] for s in llms)


def test_the_next_turn_starts_after_the_tools_it_waited_on(spans) -> None:
    with span("argus.review", "chain"):
        rec = TraceRecorder(clock=Clock())
        rec.on_assistant(turn("m1"))
        rec.on_tool_start(pre("Read", "tu-r", file_path="a.py"))
        rec.on_tool_end(pre("Read", "tu-r"))
        rec.on_assistant(turn("m2"))
        rec.close()

    finished = spans.get_finished_spans()
    (read,) = named(finished, "Read")
    m2 = max(named(finished, "llm"), key=lambda s: s.start_time)
    assert m2.start_time >= read.end_time


def test_a_message_without_a_message_id_is_its_own_turn(spans) -> None:
    with span("argus.review", "chain"):
        rec = TraceRecorder(clock=Clock())
        rec.on_assistant(turn(None))  # type: ignore[arg-type]
        (llm,) = named(spans.get_finished_spans(), "llm")  # flushed at once, not pending
        rec.on_assistant(turn(None))  # type: ignore[arg-type]
        rec.close()

    assert llm.attributes["gen_ai.usage.input_tokens"] == 102
    assert len(named(spans.get_finished_spans(), "llm")) == 2


def test_a_tool_from_an_unclaimed_agent_id_hangs_under_the_stage(spans) -> None:
    with span("argus.review", "chain") as stage:
        rec = TraceRecorder(clock=Clock())
        rec.on_tool_start(pre("Read", "tu-r", "ag-unknown", file_path="a.py"))
        rec.on_tool_end(pre("Read", "tu-r", "ag-unknown"))
        rec.close()

    (read,) = named(spans.get_finished_spans(), "Read")
    assert read.parent.span_id == stage.get_span_context().span_id


def test_a_turn_with_an_unknown_parent_hangs_under_the_stage(spans) -> None:
    with span("argus.review", "chain") as stage:
        rec = TraceRecorder(clock=Clock())
        rec.on_assistant(turn("m1", parent="tu-never-seen"))
        rec.close()

    (llm,) = named(spans.get_finished_spans(), "llm")
    assert llm.parent.span_id == stage.get_span_context().span_id


def test_content_mode_redacts_the_model_text(spans) -> None:
    with span("argus.review", "chain"):
        rec = TraceRecorder(content=True, clock=Clock())
        rec.on_assistant(turn("m1", text="found ghp_abcdefghijkl1234 in the diff"))
        rec.close()

    (llm,) = named(spans.get_finished_spans(), "llm")
    assert "ghp_abcdefghijkl1234" not in llm.attributes["output.value"]
    assert "[redacted]" in llm.attributes["output.value"]


def test_a_broken_recorder_never_raises_out_of_a_public_method(monkeypatch) -> None:
    """Tracing must never change a review's outcome: every public method swallows its own bugs."""
    stream = io.StringIO()
    telemetry.configure(log_format="json", stream=stream)

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    with span("argus.review", "chain"):
        # on_tool_start and on_assistant both open spans through tracer(); break that.
        monkeypatch.setattr(trace_module, "tracer", boom)
        rec = TraceRecorder(clock=Clock())
        assert rec.on_tool_start(pre("Read", "tu-1", file_path="a.py")) is None
        assert rec.on_assistant(turn("m1")) is None
        monkeypatch.undo()

        # on_tool_end and close: the span opened fine, but finishing it fails.
        rec2 = TraceRecorder(clock=Clock())
        rec2.on_tool_start(pre("Read", "tu-2", file_path="a.py"))
        monkeypatch.setattr(rec2, "_finish", boom)
        assert rec2.on_tool_end(pre("Read", "tu-2")) is None

        rec3 = TraceRecorder(clock=Clock())
        rec3.on_tool_start(pre("Read", "tu-3", file_path="a.py"))
        monkeypatch.setattr(rec3, "_finish", boom)
        assert rec3.close() is None

        # on_tool_denied: its own call to on_tool_start fails to seed the span it then tags.
        rec4 = TraceRecorder(clock=Clock())
        monkeypatch.setattr(rec4, "on_tool_start", boom)
        assert rec4.on_tool_denied(pre("Bash", "tu-4", command="rm -rf /"), "denied") is None

        # on_subagent_start: the span it needs to tag raises on the way in.
        rec5 = TraceRecorder(clock=Clock())
        rec5.on_tool_start(pre("Agent", "tu-5", subagent_type="security"))
        monkeypatch.setattr(rec5._spans["tu-5"], "set_attributes", boom)
        assert rec5.on_subagent_start({"agent_id": "ag-1", "agent_type": "security"}) is None

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    failures = {e["method"] for e in events if e["event"] == "trace_record_failed"}
    assert failures == {
        "on_tool_start",
        "on_assistant",
        "on_tool_end",
        "close",
        "on_tool_denied",
        "on_subagent_start",
    }
