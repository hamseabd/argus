from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock

from argus.agent.hooks import HookState
from argus.agent.ledger import AgentLedger
from argus.domain.models import AgentMetrics


def message(text: str, **fields) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text)], model="m", **fields)


def test_the_chunks_of_one_api_turn_count_once() -> None:
    ledger = AgentLedger()
    usage = {"output_tokens": 10, "cache_read_input_tokens": 100}

    ledger.record(message("thinking", message_id="msg-1", usage=usage))
    ledger.record(message("tool call", message_id="msg-1", usage=usage))
    ledger.record(message("next turn", message_id="msg-2", usage=usage))

    assert ledger.metrics(HookState()) == [
        AgentMetrics(agent="lead", turns=2, output_tokens=20, cache_read_input_tokens=200)
    ]


def test_messages_without_an_id_each_count_as_a_turn() -> None:
    ledger = AgentLedger()

    ledger.record(message("one"))
    ledger.record(message("two"))

    assert ledger.metrics(HookState())[0].turns == 2


def test_a_subagent_the_lead_never_named_is_listed_as_agent() -> None:
    ledger = AgentLedger()
    ledger.record(
        AssistantMessage(
            content=[ToolUseBlock(id="tu-1", name="Agent", input={"subagent_type": "security"})],
            model="m",
        )
    )
    ledger.record(message("from somewhere else", parent_tool_use_id="tu-unknown"))

    assert [a.agent for a in ledger.metrics(HookState())] == ["lead", "security", "agent"]
