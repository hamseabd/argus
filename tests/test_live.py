"""Opt-in end-to-end run against the real SDK: `uv run pytest -m live -s`."""

import asyncio
from pathlib import Path

import pytest
import typer

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
