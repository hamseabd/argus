from claude_agent_sdk import AssistantMessage, TextBlock, ThinkingBlock
from opentelemetry.trace import StatusCode

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


def record_a_delegation(content: bool = False):
    with span("argus.review", "chain") as stage:
        rec = TraceRecorder(content=content, clock=Clock())
        rec.on_assistant(turn("m1", text="delegating"))
        rec.on_tool_start(pre("Agent", "tu-a", subagent_type="correctness", prompt="review it"))
        rec.on_subagent_start({"agent_id": "ag-1", "agent_type": "correctness"})
        rec.on_assistant(turn("m2", parent="tu-a"))
        rec.on_tool_start(pre("Grep", "tu-g", "ag-1", pattern="ghp_abcdefghijkl1234"))
        rec.on_tool_end(pre("Grep", "tu-g", "ag-1"))
        rec.on_tool_start(pre("Read", "tu-r", "ag-1", file_path="nope.py"))
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


def test_a_tool_start_owned_by_the_same_agent_flushes_the_pending_turn(spans) -> None:
    with span("argus.review", "chain"):
        rec = TraceRecorder(content=True, clock=Clock())
        rec.on_tool_start(pre("Agent", "tu-a", subagent_type="correctness"))
        rec.on_subagent_start({"agent_id": "ag-1", "agent_type": "correctness"})
        rec.on_assistant(turn("m2", parent="tu-a", text="partial"))
        assert not named(spans.get_finished_spans(), "llm")  # still open
        rec.on_tool_start(pre("Read", "tu-r", "ag-1", file_path="a.py"))
        (llm,) = named(spans.get_finished_spans(), "llm")
        assert llm.attributes["output.value"] == "partial"
        rec.close()
