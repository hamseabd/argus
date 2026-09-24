import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

from argus import cli, tracing
from argus.domain.errors import AgentRunError
from argus.domain.models import (
    ChangedFile,
    Finding,
    Review,
    ReviewContext,
    ReviewResult,
    StageMetrics,
)

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
        return ReviewContext(
            source="local",
            repo_root=tmp_path,
            diff_text=calls.get("diff_text", "+x\n"),
            files=calls.get("files", [ChangedFile(path="a.py", status="modified")]),
        )

    async def fake_run_review(context, agent, *, verify=True, verify_concurrency=4, run_id=None):
        calls["run_id"] = run_id
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

    assert runner.invoke(cli.app, ["review", "--diff", "--fail-on", "medium"]).exit_code == 3
    assert runner.invoke(cli.app, ["review", "--diff", "--fail-on", "high"]).exit_code == 0
    assert runner.invoke(cli.app, ["review", "--diff", "--fail-on", "low"]).exit_code == 3


def test_fail_on_ignores_rejected_findings(stubbed: dict) -> None:
    stubbed["outcome"] = fake_result([finding("critical", status="rejected")])

    assert runner.invoke(cli.app, ["review", "--diff", "--fail-on", "low"]).exit_code == 0


def test_fail_on_rejects_unknown_severity(stubbed: dict) -> None:
    result = runner.invoke(cli.app, ["review", "--diff", "--fail-on", "urgent"])

    assert result.exit_code == 2  # Click's usage-error code, distinct from the gate's 3
    assert "urgent" in result.output


def test_gate_and_usage_exit_codes_differ() -> None:
    assert cli.EXIT_GATE != cli.EXIT_USAGE


def test_invalid_settings_fail_cleanly(stubbed: dict, monkeypatch) -> None:
    monkeypatch.setenv("ARGUS_VERIFY_CONCURRENCY", "abc")

    result = runner.invoke(cli.app, ["review", "--diff"])

    assert result.exit_code == 1
    assert "verify_concurrency" in result.output
    assert "Traceback" not in result.output
    assert "agent" not in stubbed


def test_every_log_event_carries_the_same_run_id(stubbed: dict, tmp_path: Path) -> None:
    stubbed["outcome"] = fake_result([finding("low")])

    result = runner.invoke(cli.app, ["review", "--diff", "--json", str(tmp_path / "r.json")])

    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
    assert "artifact_written" in [e["event"] for e in events]
    assert len({e.get("run_id") for e in events}) == 1
    assert None not in {e.get("run_id") for e in events}


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


def test_an_empty_diff_fails_before_any_query(stubbed: dict) -> None:
    stubbed["diff_text"] = ""
    stubbed["files"] = []

    result = runner.invoke(cli.app, ["review", "--diff"])

    assert result.exit_code == 1
    assert "nothing to review" in result.output
    assert "agent" not in stubbed


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    events: list[str] = []

    @contextmanager
    def fake_session(environ=None):
        events.append("in")
        try:
            yield False
        finally:
            events.append("out")

    monkeypatch.setattr(tracing, "session", fake_session)
    return events


def test_the_review_runs_inside_a_tracing_session(stubbed: dict, sessions: list[str]) -> None:
    result = runner.invoke(cli.app, ["review", "--diff"])

    assert result.exit_code == 0, result.output
    assert sessions == ["in", "out"]


def test_a_failed_review_still_closes_the_tracing_session(
    stubbed: dict, sessions: list[str]
) -> None:
    stubbed["outcome"] = AgentRunError("error_max_budget_usd", 3.0, "sess")

    result = runner.invoke(cli.app, ["review", "--diff"])

    assert result.exit_code == 1
    assert sessions == ["in", "out"]
