import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from argus import cli
from argus.domain.errors import GitHubError
from argus.domain.models import (
    ChangedFile,
    Finding,
    PRInfo,
    Review,
    ReviewContext,
    ReviewResult,
    StageMetrics,
)

runner = CliRunner()
PR = PRInfo(
    owner="o",
    repo="r",
    number=7,
    title="t",
    body="",
    base_ref="main",
    head_ref="f",
    head_sha="a" * 40,
    html_url="https://github.com/o/r/pull/7",
)


def fake_result() -> ReviewResult:
    finding = Finding(
        id="high-1",
        file="a.py",
        line=1,
        severity="high",
        category="correctness",
        title="p",
        description="d",
        evidence="e",
        confidence=0.9,
        status="confirmed",
    )
    return ReviewResult(
        review=Review(summary="s", files_reviewed=["a.py"], findings=[finding]),
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
            )
        ],
        total_cost_usd=0.5,
        session_id="sess",
    )


class FakeGitHub:
    def __init__(self, calls: dict) -> None:
        self.calls = calls

    def post_review(self, owner: str, repo: str, number: int, payload: dict) -> str:
        self.calls["posted"] = (owner, repo, number, payload)
        if isinstance(self.calls.get("post_outcome"), Exception):
            raise self.calls["post_outcome"]
        return "https://github.com/o/r/pull/7#pullrequestreview-9"


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    calls: dict = {}

    def fake_client(token: str) -> FakeGitHub:
        calls["token"] = token
        return FakeGitHub(calls)

    def fake_pr_context(client, owner, repo, number, repo_root, max_bytes=0) -> ReviewContext:
        calls["pr"] = (owner, repo, number)
        calls["root"] = repo_root
        return ReviewContext(
            source="pr",
            repo_root=tmp_path,
            diff_text="",
            files=[ChangedFile(path="a.py", status="modified")],
            pr=PR,
        )

    async def fake_run_review(context, agent, *, verify=True, verify_concurrency=4, run_id=None):
        calls["context"] = context
        return fake_result()

    monkeypatch.setattr(cli, "GitHubClient", fake_client)
    monkeypatch.setattr(cli, "pr_context", fake_pr_context)
    monkeypatch.setattr(cli, "run_review", fake_run_review)
    monkeypatch.setattr(cli, "head_sha", lambda path: "a" * 40)
    monkeypatch.setattr(cli, "repo_root", lambda path: tmp_path / "toplevel")
    monkeypatch.setattr(cli, "repo_from_remote", lambda path: None)
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    return calls


def test_pr_and_diff_are_mutually_exclusive(stubbed: dict) -> None:
    result = runner.invoke(cli.app, ["review", "--pr", "7", "--diff", "--repo", "o/r"])

    assert result.exit_code == 2
    assert "--pr" in result.output and "--diff" in result.output


def test_pr_mode_reviews_and_prints_without_posting(stubbed: dict) -> None:
    result = runner.invoke(cli.app, ["review", "--pr", "7", "--repo", "o/r"])

    assert result.exit_code == 0, result.output
    assert stubbed["pr"] == ("o", "r", 7)
    assert stubbed["token"] == "ghs_test"
    assert stubbed["context"].source == "pr"
    assert stubbed["root"].name == "toplevel"  # the git top level, not the working directory
    assert "# Argus review" in result.output
    assert "posted" not in stubbed


def test_repo_defaults_to_github_repository_then_origin(stubbed: dict, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "env/repo")
    assert runner.invoke(cli.app, ["review", "--pr", "7"]).exit_code == 0
    assert stubbed["pr"] == ("env", "repo", 7)

    monkeypatch.delenv("GITHUB_REPOSITORY")
    monkeypatch.setattr(cli, "repo_from_remote", lambda path: "remote/repo")
    assert runner.invoke(cli.app, ["review", "--pr", "7"]).exit_code == 0
    assert stubbed["pr"] == ("remote", "repo", 7)


def test_pr_without_a_resolvable_repo_fails_before_any_query(stubbed: dict) -> None:
    result = runner.invoke(cli.app, ["review", "--pr", "7"])

    assert result.exit_code == 1
    assert "--repo" in result.output
    assert "context" not in stubbed


def test_pr_without_a_github_token_fails_before_any_query(stubbed: dict, monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN")

    result = runner.invoke(cli.app, ["review", "--pr", "7", "--repo", "o/r"])

    assert result.exit_code == 1
    assert "GITHUB_TOKEN" in result.output
    assert "context" not in stubbed


def test_post_requires_pr(stubbed: dict) -> None:
    result = runner.invoke(cli.app, ["review", "--diff", "--post"])

    assert result.exit_code == 2
    assert "--post" in result.output


def test_post_sends_the_review_and_reports_the_url(stubbed: dict) -> None:
    result = runner.invoke(cli.app, ["review", "--pr", "7", "--repo", "o/r", "--post"])

    assert result.exit_code == 0, result.output
    owner, repo, number, payload = stubbed["posted"]
    assert (owner, repo, number) == ("o", "r", 7)
    assert payload["commit_id"] == "a" * 40
    assert payload["event"] == "COMMENT"
    assert "pullrequestreview-9" in result.output


def test_posting_failure_still_writes_the_json_and_exits_one(stubbed: dict, tmp_path: Path) -> None:
    stubbed["post_outcome"] = GitHubError(422, '{"message": "Validation Failed"}')
    out = tmp_path / "r.json"

    result = runner.invoke(
        cli.app, ["review", "--pr", "7", "--repo", "o/r", "--post", "--json", str(out)]
    )

    assert result.exit_code == 1
    assert "422" in result.output
    assert json.loads(out.read_text())["session_id"] == "sess"


def test_head_mismatch_is_a_warning_not_an_error(stubbed: dict, monkeypatch) -> None:
    monkeypatch.setattr(cli, "head_sha", lambda path: "b" * 40)

    result = runner.invoke(cli.app, ["review", "--pr", "7", "--repo", "o/r"])

    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
    assert any(e["event"] == "head_mismatch" for e in events)


def test_fail_on_applies_in_pr_mode(stubbed: dict) -> None:
    assert (
        runner.invoke(
            cli.app, ["review", "--pr", "7", "--repo", "o/r", "--fail-on", "high"]
        ).exit_code
        == 3
    )
