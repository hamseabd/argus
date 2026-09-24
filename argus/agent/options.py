"""ClaudeAgentOptions for the lead reviewer and the verifier.

Everything that shapes a query is decided here from Settings and the
review context: models, efforts, caps, the read-only tool set, the
specialist subagents, the structured-output schema, the hooks, and the
session isolation that keeps the repository under review from injecting
its own settings, hooks, or CLAUDE.md into the reviewer.
"""

from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions

from argus.agent.hooks import ALLOWED_TOOLS, DENIED_TOOLS, HookState, build_hooks
from argus.agent.prompts import load_prompt
from argus.agent.schemas import output_format, review_schema, verdict_schema
from argus.agent.tools import GIT_HISTORY_TOOL_NAME, SERVER_NAME, build_argus_server
from argus.domain.models import ReviewContext
from argus.settings import Settings
from argus.telemetry import get_logger

SPECIALISTS: tuple[str, ...] = ("correctness", "security", "quality")
_SPECIALIST_FOCUS = {
    "correctness": "logic errors, edge cases, error paths, concurrency, resource leaks",
    "security": (
        "injection, secrets, authorization gaps, unsafe deserialization, SSRF, path traversal"
    ),
    "quality": "missing or weak tests for changed behavior, dead code, API misuse",
}
_READ_TOOLS = ["Read", "Grep", "Glob"]


def specialist_agents(settings: Settings) -> dict[str, AgentDefinition]:
    return {
        name: AgentDefinition(
            description=f"{name} specialist: {_SPECIALIST_FOCUS[name]}. Read-only.",
            prompt=load_prompt(name),
            tools=[*_READ_TOOLS, GIT_HISTORY_TOOL_NAME],
            model=settings.specialist_model,
            effort=settings.specialist_effort,
            maxTurns=settings.specialist_max_turns,
            # Foreground: the lead's Agent call returns with the findings, so it
            # cannot answer while a specialist is still running.
            background=False,
        )
        for name in SPECIALISTS
    }


def lead_options(
    settings: Settings, context: ReviewContext, state: HookState
) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=settings.lead_model,
        effort=settings.lead_effort,
        max_turns=settings.lead_max_turns,
        max_budget_usd=settings.lead_max_budget_usd,
        system_prompt=load_prompt("lead"),
        tools=[*_READ_TOOLS, "Agent"],
        allowed_tools=list(ALLOWED_TOOLS),
        agents=specialist_agents(settings),
        output_format=output_format(review_schema()),
        **_common(context, state),
    )


def verifier_options(
    settings: Settings, context: ReviewContext, state: HookState
) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=settings.verifier_model,
        effort=settings.verifier_effort,
        max_turns=settings.verifier_max_turns,
        max_budget_usd=settings.verifier_max_budget_usd,
        system_prompt=load_prompt("verifier"),
        tools=list(_READ_TOOLS),
        allowed_tools=[*_READ_TOOLS, GIT_HISTORY_TOOL_NAME],
        output_format=output_format(verdict_schema()),
        **_common(context, state),
    )


def _common(context: ReviewContext, state: HookState) -> dict:
    return {
        "cwd": context.repo_root,
        "permission_mode": "dontAsk",
        "disallowed_tools": sorted(DENIED_TOOLS),
        "hooks": build_hooks(state),
        "mcp_servers": {SERVER_NAME: build_argus_server(context.repo_root)},
        "strict_mcp_config": True,
        "setting_sources": [],
        "stderr": _cli_stderr,
        # Argus builds its own spans (argus.tracing); the native ones carry the
        # account's identity and would duplicate the tree. The exporter settings
        # are blanked too: the headers carry the LangSmith key, and the child
        # process has no use for it.
        "env": {
            "CLAUDE_CODE_ENABLE_TELEMETRY": "0",
            "OTEL_EXPORTER_OTLP_HEADERS": "",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "",
        },
    }


def _cli_stderr(line: str) -> None:
    get_logger().debug("cli_stderr", line=line.rstrip())
