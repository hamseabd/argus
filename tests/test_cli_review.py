import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from argus import cli
from argus.domain.errors import AgentRunError
from argus.domain.models import Finding, Review, ReviewContext, ReviewResult, StageMetrics

runner = CliRunner()


def finding(severity: str, status: str = "confirmed") -> Finding:
    return Finding(
        id=f"{severity}-1",
        file="a.py",
        line=1,
        severity=severity,
        category="correctness",
        title=f"{severity} problem",
        description="d",
        evidence="e",
        confidence=0.9,
        status=status,
    )


def fake_result(findings: list[Finding]) -> ReviewResult:
    return ReviewResult(
        review=Review(summary="s", files_reviewed=["a.py"], findings=findings),
        verdicts=[],
        metrics=[
            StageMetrics(
                stage="review",
                model="m",
                cost_usd=0.5,
                input_tokens=1,
                output_tokens=1,
                num_turns=1,
                duration_ms=1,
                subagents_run=3,
            )
        ],
        total_cost_usd=0.5,
        session_id="sess",
    )


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Stub the context and pipeline; record what the command passed to them."""
    calls: dict = {}

    def fake_local_context(path: Path, base: str = "main", max_bytes: int = 0) -> ReviewContext:
        calls["base"] = base
        calls["max_bytes"] = max_bytes
        return ReviewContext(source="local", repo_root=tmp_path, diff_text="", files=[])

    async def fake_run_review(context, agent, *, verify=True, verify_concurrency=4):
        calls["verify"] = verify
        calls["verify_concurrency"] = verify_concurrency
        calls["agent"] = agent
        if isinstance(calls.get("outcome"), Exception):
            raise calls["outcome"]
        return calls.get("outcome") or fake_result([])

    monkeypatch.setattr(cli, "local_context", fake_local_context)
    monkeypatch.setattr(cli, "run_review", fake_run_review)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    return calls


def test_review_requires_a_mode() -> None:
    result = runner.invoke(cli.app, ["review"])

    assert result.exit_code != 0
    assert "--diff" in result.output


def test_diff_review_prints_the_report_and_exits_zero(stubbed: dict) -> None:
    stubbed["outcome"] = fake_result([finding("low")])

    result = runner.invoke(cli.app, ["review", "--diff"])

    assert result.exit_code == 0, result.output
    assert "# Argus review" in result.output
    assert "low problem" in result.output
    assert stubbed["base"] == "main"
    assert stubbed["verify"] is True


def test_base_no_verify_and_settings_reach_the_pipeline(stubbed: dict, monkeypatch) -> None:
    monkeypatch.setenv("ARGUS_VERIFY_CONCURRENCY", "2")
    monkeypatch.setenv("ARGUS_DIFF_SIZE_CAP", "1234")

    result = runner.invoke(cli.app, ["review", "--diff", "--base", "develop", "--no-verify"])

    assert result.exit_code == 0, result.output
    assert stubbed["base"] == "develop"
    assert stubbed["verify"] is False
    assert stubbed["verify_concurrency"] == 2
    assert stubbed["max_bytes"] == 1234


def test_json_artifact_is_written(stubbed: dict, tmp_path: Path) -> None:
    stubbed["outcome"] = fake_result([finding("high")])
    out = tmp_path / "out" / "argus-review.json"

    result = runner.invoke(cli.app, ["review", "--diff", "--json", str(out)])

    assert result.exit_code == 0, result.output
    data = json.loads(out.read_text())
    assert data["review"]["findings"][0]["id"] == "high-1"
    assert data["total_cost_usd"] == 0.5


def test_fail_on_trips_for_confirmed_or_unverified_at_or_above_the_severity(stubbed: dict) -> None:
    stubbed["outcome"] = fake_result([finding("medium", status="unverified")])

    assert runner.invoke(cli.app, ["review", "--diff", "--fail-on", "medium"]).exit_code == 2
    assert runner.invoke(cli.app, ["review", "--diff", "--fail-on", "high"]).exit_code == 0
    assert runner.invoke(cli.app, ["review", "--diff", "--fail-on", "low"]).exit_code == 2


def test_fail_on_ignores_rejected_findings(stubbed: dict) -> None:
    stubbed["outcome"] = fake_result([finding("critical", status="rejected")])

    assert runner.invoke(cli.app, ["review", "--diff", "--fail-on", "low"]).exit_code == 0


def test_fail_on_rejects_unknown_severity(stubbed: dict) -> None:
    result = runner.invoke(cli.app, ["review", "--diff", "--fail-on", "urgent"])

    assert result.exit_code == 2  # typer usage error
    assert "urgent" in result.output


def test_agent_errors_exit_one_with_the_subtype_and_cost(stubbed: dict) -> None:
    stubbed["outcome"] = AgentRunError("error_max_budget_usd", 3.0, "sess")

    result = runner.invoke(cli.app, ["review", "--diff"])

    assert result.exit_code == 1
    assert "error_max_budget_usd" in result.output
    assert "3.00" in result.output


def test_malformed_credential_fails_before_any_query(stubbed: dict, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-api03-wrong-prefix-" + "a" * 60)

    result = runner.invoke(cli.app, ["review", "--diff"])

    assert result.exit_code == 1
    assert "sk-ant-oat" in result.output
    assert "agent" not in stubbed


def test_git_errors_exit_one(stubbed: dict, monkeypatch) -> None:
    from argus.domain.errors import GitError

    def broken(path, base="main", max_bytes=0):
        raise GitError("git merge-base failed: fatal: Not a valid object name nope")

    monkeypatch.setattr(cli, "local_context", broken)

    result = runner.invoke(cli.app, ["review", "--diff", "--base", "nope"])

    assert result.exit_code == 1
    assert "nope" in result.output
