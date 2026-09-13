"""Opt-in end-to-end run against the real SDK: `uv run pytest -m live -s`."""

import asyncio
from pathlib import Path

import pytest
import typer
from claude_agent_sdk import ClaudeAgentOptions, query

from argus.agent.hooks import HookState, build_hooks
from argus.agent.review import SdkReviewAgent
from argus.context.git import local_context
from argus.pipeline import run_review
from argus.report.markdown import render_report
from argus.settings import Settings
from tests.helpers import SEEDED_FILES, build_seeded_repo

pytestmark = pytest.mark.live


def test_seeded_bugs_are_found_and_confirmed(tmp_path: Path, capsys) -> None:
    repo = build_seeded_repo(tmp_path / "seeded")
    context = local_context(repo, base="main")

    result = asyncio.run(run_review(context, SdkReviewAgent(Settings())))

    with capsys.disabled():
        typer.echo(render_report(result))
    confirmed = [f for f in result.review.findings if f.status == "confirmed"]
    assert confirmed, "expected at least one confirmed finding"
    assert {f.file for f in confirmed} & set(SEEDED_FILES)


def test_the_runtime_honours_a_refused_read(tmp_path: Path) -> None:
    """The deny path, against the real SDK.

    A review never reproduces this on demand: the lead delegates and answers
    without reading, which is the point of the budget. So the hook is driven
    on its own, with a budget of zero and a prompt whose only move is a read.
    What is under test is the contract, that the runtime asks our PreToolUse
    hook and obeys a deny, so an SDK upgrade cannot quietly drop the cap.
    """
    (tmp_path / "secret.txt").write_text("the answer is 41\n", encoding="utf-8")
    state = HookState(read_budget=0)
    options = ClaudeAgentOptions(
        model="claude-sonnet-5",
        effort="low",
        max_turns=3,
        system_prompt="Answer with the contents of the file the user names.",
        tools=["Read"],
        allowed_tools=["Read"],
        permission_mode="dontAsk",
        cwd=tmp_path,
        hooks=build_hooks(state),
        setting_sources=[],
        strict_mcp_config=True,
    )

    async def ask() -> None:
        async for _ in query(prompt="What does secret.txt say?", options=options):
            pass

    asyncio.run(ask())

    assert state.reads_denied >= 1, "the read should have been refused"
    assert state.lead_reads == 0
