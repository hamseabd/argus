from pathlib import Path

from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions

from argus.agent.hooks import ALLOWED_TOOLS, DENIED_TOOLS, HookState
from argus.agent.options import SPECIALISTS, lead_options, specialist_agents, verifier_options
from argus.agent.schemas import review_schema, verdict_schema
from argus.agent.tools import GIT_HISTORY_TOOL_NAME
from argus.domain.models import ReviewContext
from argus.settings import Settings


def context(tmp_path: Path) -> ReviewContext:
    return ReviewContext(source="local", repo_root=tmp_path, diff_text="", files=[])


def settings() -> Settings:
    return Settings(_env_file=None)


def test_lead_options_follow_the_spec(tmp_path: Path) -> None:
    opts = lead_options(settings(), context(tmp_path), HookState())

    assert isinstance(opts, ClaudeAgentOptions)
    assert opts.model == "claude-opus-5"
    assert opts.effort == "high"
    assert opts.max_turns == 40
    assert opts.max_budget_usd == 3.0
    assert opts.permission_mode == "dontAsk"
    assert opts.cwd == tmp_path
    assert set(opts.allowed_tools) == set(ALLOWED_TOOLS)
    assert opts.tools == ["Read", "Grep", "Glob", "Agent"]
    assert set(opts.disallowed_tools) >= DENIED_TOOLS
    assert opts.output_format == {"type": "json_schema", "schema": review_schema()}
    assert isinstance(opts.system_prompt, str) and "all three" in opts.system_prompt
    assert set(opts.agents) == set(SPECIALISTS)
    assert set(opts.hooks) == {
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "SubagentStart",
        "SubagentStop",
    }
    assert opts.mcp_servers["argus"]["type"] == "sdk"
    assert opts.strict_mcp_config is True


def test_lead_options_isolate_the_session_from_the_target_repo_settings(tmp_path: Path) -> None:
    opts = lead_options(settings(), context(tmp_path), HookState())

    # A repository under review must not be able to inject settings, hooks, or CLAUDE.md.
    assert opts.setting_sources == []


def test_specialists_are_read_only_sonnet_agents() -> None:
    agents = specialist_agents(settings())

    assert list(agents) == ["correctness", "security", "quality"]
    for name, agent in agents.items():
        assert isinstance(agent, AgentDefinition)
        assert agent.model == "claude-sonnet-5"
        assert agent.effort == "medium"
        assert agent.maxTurns == 25
        assert agent.tools == ["Read", "Grep", "Glob", GIT_HISTORY_TOOL_NAME]
        assert "Agent" not in agent.tools
        assert name in agent.description
        assert len(agent.prompt) > 200


def test_verifier_options_follow_the_spec(tmp_path: Path) -> None:
    opts = verifier_options(settings(), context(tmp_path), HookState())

    assert opts.model == "claude-sonnet-5"
    assert opts.effort == "medium"
    assert opts.max_turns == 10
    assert opts.max_budget_usd == 0.5
    assert opts.permission_mode == "dontAsk"
    assert opts.tools == ["Read", "Grep", "Glob"]
    assert "Agent" not in opts.allowed_tools
    assert GIT_HISTORY_TOOL_NAME in opts.allowed_tools
    assert opts.agents is None
    assert opts.output_format == {"type": "json_schema", "schema": verdict_schema()}
    assert opts.setting_sources == []
    assert "refute" in opts.system_prompt


def test_settings_overrides_reach_the_options(tmp_path: Path) -> None:
    custom = Settings(_env_file=None, lead_model="claude-sonnet-5", lead_max_budget_usd=1.0)

    opts = lead_options(custom, context(tmp_path), HookState())

    assert opts.model == "claude-sonnet-5"
    assert opts.max_budget_usd == 1.0


def test_native_claude_code_telemetry_is_forced_off(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    for options in (
        lead_options(settings(), ctx, HookState()),
        verifier_options(settings(), ctx, HookState()),
    ):
        assert options.env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "0"


def test_the_trace_exporter_settings_never_reach_the_cli(tmp_path: Path) -> None:
    """The OTLP headers carry the LangSmith key; the Claude Code child has no use for it."""
    ctx = context(tmp_path)
    for options in (
        lead_options(settings(), ctx, HookState()),
        verifier_options(settings(), ctx, HookState()),
    ):
        assert options.env["OTEL_EXPORTER_OTLP_HEADERS"] == ""
        assert options.env["OTEL_EXPORTER_OTLP_ENDPOINT"] == ""
