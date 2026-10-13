import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from argus import cli
from argus.domain.errors import GitHubError
from argus.domain.models import (
    ChangedFile,
    Finding,
    Increment,
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

    def pr_reviews(self, owner: str, repo: str, number: int) -> list[dict]:
        self.calls["fetched_known"] = True
        return self.calls.get("reviews", [])

    def pr_review_comments(self, owner: str, repo: str, number: int) -> list[dict]:
        return self.calls.get("comments", [])

    def file_text(self, owner: str, repo: str, path: str, ref: str) -> str:
        raise GitHubError(404, "Not Found")

    def create_check_run(self, owner: str, repo: str, payload: dict) -> str:
        self.calls["check"] = (owner, repo, payload)
        return "https://github.com/o/r/runs/5"

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

    def fake_pr_context(
        client, owner, repo, number, repo_root, max_bytes=0, ignore=()
    ) -> ReviewContext:
        calls["pr"] = (owner, repo, number)
        calls["root"] = repo_root
        calls["ignore"] = ignore
        return ReviewContext(
            source="pr",
            repo_root=tmp_path,
            diff_text="",
            files=[ChangedFile(path="a.py", status="modified")],
            pr=PR,
        )

    async def fake_run_review(
        context,
        agent,
        *,
        verify=True,
        verify_concurrency=4,
        run_id=None,
        known=(),
        config=None,
    ):
        calls["context"] = context
        calls["known"] = list(known)
        return calls.get("outcome") or fake_result()

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


def test_ignore_patterns_reach_the_pull_request_context(stubbed: dict, monkeypatch) -> None:
    monkeypatch.setenv("ARGUS_IGNORE_PATHS", "*.lock")

    result = runner.invoke(cli.app, ["review", "--pr", "7", "--repo", "o/r", "--ignore", "vendor/"])

    assert result.exit_code == 0, result.output
    assert tuple(stubbed["ignore"]) == ("*.lock", "vendor/")


def test_pr_mode_hands_the_findings_argus_already_posted_to_the_pipeline(
    stubbed: dict, monkeypatch
) -> None:
    monkeypatch.setenv("ARGUS_REVIEWER_LOGIN", "argus-code-reviewer-agent[bot]")
    stubbed["reviews"] = [
        {
            "id": 1,
            "body": "<!-- argus:review -->\n\nEarlier.",
            "user": {"login": "argus-code-reviewer-agent[bot]", "type": "Bot"},
            "html_url": "https://github.com/o/r/pull/7#r1",
        }
    ]
    stubbed["comments"] = [
        {
            "pull_request_review_id": 1,
            "path": "a.py",
            "line": 3,
            "original_line": 3,
            "body": "**[HIGH] Stale entry** · correctness · confirmed · confidence 0.80",
            "html_url": "https://github.com/o/r/pull/7#discussion_1",
            "user": {"login": "argus-code-reviewer-agent[bot]", "type": "Bot"},
        }
    ]

    result = runner.invoke(cli.app, ["review", "--pr", "7", "--repo", "o/r"])

    assert result.exit_code == 0, result.output
    assert [(k.file, k.line, k.title) for k in stubbed["known"]] == [("a.py", 3, "Stale entry")]


def test_include_known_skips_the_lookup_and_reports_everything(stubbed: dict, monkeypatch) -> None:
    # Reporting what was already posted only makes sense over the whole pull
    # request, so it also turns the incremental review off.
    monkeypatch.setenv("ARGUS_REVIEWER_LOGIN", "argus-code-reviewer-agent[bot]")

    result = runner.invoke(cli.app, ["review", "--pr", "7", "--repo", "o/r", "--include-known"])

    assert result.exit_code == 0, result.output
    assert "fetched_known" not in stubbed
    assert stubbed["known"] == []


def test_without_a_reviewer_identity_no_earlier_review_is_trusted(
    stubbed: dict, monkeypatch
) -> None:
    monkeypatch.delenv("ARGUS_REVIEWER_LOGIN", raising=False)

    result = runner.invoke(cli.app, ["review", "--pr", "7", "--repo", "o/r"])

    assert result.exit_code == 0, result.output
    assert "fetched_known" not in stubbed
    assert stubbed["known"] == []


def test_summary_records_the_posted_review_url(stubbed: dict, tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"

    result = runner.invoke(
        cli.app, ["review", "--pr", "7", "--repo", "o/r", "--post", "--summary", str(summary)]
    )

    assert result.exit_code == 0, result.output
    text = summary.read_text()
    assert "## Argus review" in text
    assert text.rstrip().endswith("Posted: https://github.com/o/r/pull/7#pullrequestreview-9")


def test_summary_survives_a_posting_failure_without_a_url(stubbed: dict, tmp_path: Path) -> None:
    stubbed["post_outcome"] = GitHubError(422, "nope")
    summary = tmp_path / "summary.md"

    result = runner.invoke(
        cli.app, ["review", "--pr", "7", "--repo", "o/r", "--post", "--summary", str(summary)]
    )

    assert result.exit_code == 1
    text = summary.read_text()
    assert "## Argus review" in text
    assert "Posted:" not in text


LOGIN = "argus-code-reviewer-agent[bot]"


def earlier_review(commit: str) -> dict:
    return {
        "id": 1,
        "commit_id": commit,
        "body": "<!-- argus:review -->\n\nEarlier.",
        "user": {"login": LOGIN, "type": "Bot"},
        "html_url": "https://github.com/o/r/pull/7#r1",
    }


@pytest.fixture
def increments(monkeypatch: pytest.MonkeyPatch, stubbed: dict) -> list:
    asked: list = []

    def fake_increment(repo, since, head, max_bytes=0, ignore=()):
        asked.append((since, head))
        if stubbed.get("increment") == "none":
            return None
        return Increment(
            since_sha=since,
            diff_text="diff --git a/b.py b/b.py\n+x\n",
            files=[ChangedFile(path="b.py", status="modified")],
        )

    monkeypatch.setattr(cli, "increment_since", fake_increment)
    monkeypatch.setenv("ARGUS_REVIEWER_LOGIN", LOGIN)
    return asked


def test_a_pull_request_reviewed_before_is_reviewed_from_where_argus_left_off(
    stubbed: dict, increments: list
) -> None:
    stubbed["reviews"] = [earlier_review("b" * 40)]

    result = runner.invoke(cli.app, ["review", "--pr", "7", "--repo", "o/r"])

    assert result.exit_code == 0, result.output
    assert increments == [("b" * 40, "a" * 40)]
    assert stubbed["context"].increment.since_sha == "b" * 40


def test_a_head_already_reviewed_is_not_reviewed_again(stubbed: dict, increments: list) -> None:
    stubbed["reviews"] = [earlier_review("a" * 40)]  # the PR head

    result = runner.invoke(cli.app, ["review", "--pr", "7", "--repo", "o/r", "--post"])

    assert result.exit_code == 0, result.output
    assert "already reviewed" in result.output
    assert "context" not in stubbed and "posted" not in stubbed
    assert increments == []


def test_when_the_reviewed_commit_is_gone_the_whole_pull_request_is_reviewed(
    stubbed: dict, increments: list
) -> None:
    stubbed["reviews"] = [earlier_review("b" * 40)]
    stubbed["increment"] = "none"  # force-pushed away

    result = runner.invoke(cli.app, ["review", "--pr", "7", "--repo", "o/r"])

    assert result.exit_code == 0, result.output
    assert stubbed["context"].increment is None


def test_full_reviews_the_whole_pull_request_but_still_skips_known_findings(
    stubbed: dict, increments: list
) -> None:
    stubbed["reviews"] = [earlier_review("b" * 40)]

    result = runner.invoke(cli.app, ["review", "--pr", "7", "--repo", "o/r", "--full"])

    assert result.exit_code == 0, result.output
    assert increments == []
    assert stubbed["context"].increment is None
    assert stubbed["fetched_known"] is True


def test_pr_review_reads_the_rules_at_the_base_branch(stubbed: dict, monkeypatch) -> None:
    asked: list = []

    def fake_rules(client, owner, repo, ref):
        asked.append((owner, repo, ref))
        return "Prefer explicit errors.\n"

    monkeypatch.setattr(cli, "rules_from_github", fake_rules)

    result = runner.invoke(cli.app, ["review", "--pr", "7", "--repo", "o/r"])

    assert result.exit_code == 0, result.output
    assert asked == [("o", "r", "main")]
    assert stubbed["context"].review_rules == "Prefer explicit errors.\n"


def critical_result() -> ReviewResult:
    base = fake_result()
    finding = base.review.findings[0].model_copy(update={"severity": "critical"})
    return base.model_copy(
        update={"review": base.review.model_copy(update={"findings": [finding]})}
    )


def test_check_posts_the_gate_as_a_check_run_and_the_job_stays_green(
    stubbed: dict, monkeypatch
) -> None:
    stubbed["outcome"] = critical_result()

    result = runner.invoke(
        cli.app, ["review", "--pr", "7", "--repo", "o/r", "--check", "--fail-on", "high"]
    )

    assert result.exit_code == 0, result.output  # the check carries the gate, not the job
    owner, repo, payload = stubbed["check"]
    assert (owner, repo, payload["conclusion"]) == ("o", "r", "failure")
    assert "Check run: https://github.com/o/r/runs/5" in result.output


def test_without_check_the_gate_is_still_the_exit_code(stubbed: dict) -> None:
    stubbed["outcome"] = critical_result()

    result = runner.invoke(cli.app, ["review", "--pr", "7", "--repo", "o/r", "--fail-on", "high"])

    assert result.exit_code == 3
    assert "check" not in stubbed


def test_check_needs_a_pull_request(stubbed: dict) -> None:
    result = runner.invoke(cli.app, ["review", "--diff", "--check"])

    assert result.exit_code == 2
    assert "--check needs --pr" in result.output


def test_with_a_gate_the_whole_pull_request_is_reviewed_again(
    stubbed: dict, increments: list
) -> None:
    # An increment would never re-find a critical from an earlier push, so a
    # gated run must see everything: reported before is not fixed.
    stubbed["reviews"] = [earlier_review("b" * 40)]

    result = runner.invoke(
        cli.app, ["review", "--pr", "7", "--repo", "o/r", "--fail-on", "critical"]
    )

    assert result.exit_code == 0, result.output
    assert increments == []
    assert stubbed["context"].increment is None


def test_with_a_gate_an_already_reviewed_head_is_still_reviewed(
    stubbed: dict, increments: list
) -> None:
    stubbed["reviews"] = [earlier_review("a" * 40)]  # the PR head

    result = runner.invoke(
        cli.app, ["review", "--pr", "7", "--repo", "o/r", "--fail-on", "critical"]
    )

    assert result.exit_code == 0, result.output
    assert "already reviewed" not in result.output
    assert "context" in stubbed


def test_a_check_run_is_posted_even_on_a_head_already_reviewed(
    stubbed: dict, increments: list
) -> None:
    # A required check must never sit pending: --check always reviews and posts.
    stubbed["reviews"] = [earlier_review("a" * 40)]  # the PR head

    result = runner.invoke(cli.app, ["review", "--pr", "7", "--repo", "o/r", "--check"])

    assert result.exit_code == 0, result.output
    assert increments == []
    assert "check" in stubbed
