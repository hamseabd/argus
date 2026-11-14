from typer.testing import CliRunner

from argus import __version__
from argus.cli import app

runner = CliRunner()


def test_version_prints_package_version() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.output.strip() == f"argus {__version__}"


def test_no_args_shows_help_and_fails() -> None:
    result = runner.invoke(app, [])

    assert result.exit_code != 0
    assert "version" in result.output


def test_feedback_joins_reactions_threads_and_handoffs_and_appends_the_table(
    monkeypatch, tmp_path
) -> None:
    from typer.testing import CliRunner

    from argus import cli

    asked: dict = {"reviews": [], "threads": []}
    comment = {
        "id": 1,
        "created_at": "2099-01-01T00:00:00Z",
        "pull_request_review_id": 50,
        "pull_request_url": "https://api.github.com/repos/o/r/pulls/7",
        "html_url": "https://github.com/o/r/pull/7#discussion_r1",
        "path": "a.py",
        "line": 1,
        "user": {"login": "argus[bot]", "type": "Bot"},
        "body": "**[HIGH] Leak** · correctness · confirmed · confidence 0.90",
        "reactions": {"+1": 0, "-1": 0},
    }
    stale = comment | {"id": 2, "created_at": "2000-01-01T00:00:00Z"}
    review = {
        "id": 50,
        "user": {"login": "argus[bot]", "type": "Bot"},
        "body": '<!-- argus:review -->\n\nS.\n<!-- argus:handoff {"version":2,"findings":'
        '[{"id":"correctness-1","specialist":"design","confidence":0.8,"comment":1}]} -->',
    }

    class FakeGitHub:
        def __init__(self, token: str) -> None:
            asked["token"] = token

        def repo_review_comments(self, owner, repo, since=None):
            asked["repo"] = (owner, repo, since)
            return [comment, stale]

        def pr_reviews(self, owner, repo, number):
            asked["reviews"].append(number)
            return [review]

        def review_threads(self, owner, repo, number):
            asked["threads"].append(number)
            return [{"comment_id": 1, "resolved": True, "outdated": False}]

    monkeypatch.setattr(cli, "GitHubClient", FakeGitHub)
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
    monkeypatch.setenv("ARGUS_REVIEWER_LOGIN", "argus[bot]")
    out = tmp_path / "feedback.json"
    log = tmp_path / "feedback.md"
    log.write_text("# Feedback\n")

    result = CliRunner().invoke(
        cli.app,
        ["feedback", "--repo", "o/r", "--days", "7", "--json", str(out), "--append", str(log)],
    )

    assert result.exit_code == 0, result.output
    owner, repo, since = asked["repo"]
    assert (owner, repo) == ("o", "r")
    assert since.endswith("Z")
    assert (asked["reviews"], asked["threads"]) == ([7], [7])
    assert "| design | 1 | 1 | 0 | 100% | 0% |" in result.output
    assert "1 findings posted inline" in result.output  # the stale comment is left out
    assert '"resolved": true' in out.read_text()
    appended = log.read_text()
    assert appended.startswith("# Feedback\n## ")
    assert ": o/r, last 7 days" in appended
    assert "| design | 1 | 1 | 0 | 100% | 0% |" in appended


def test_feedback_needs_the_identity_argus_posts_as(monkeypatch) -> None:
    from typer.testing import CliRunner

    from argus import cli

    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
    monkeypatch.delenv("ARGUS_REVIEWER_LOGIN", raising=False)

    result = CliRunner().invoke(cli.app, ["feedback", "--repo", "o/r"])

    assert result.exit_code == 1
    assert "ARGUS_REVIEWER_LOGIN" in result.output


def test_a_failure_message_is_redacted_before_it_is_printed_or_logged(capsys) -> None:
    import pytest
    import typer

    from argus import cli

    with pytest.raises(typer.Exit):
        cli._fail("GitHub said: token ghs_" + "b" * 36 + " is bad")

    assert "ghs_bbbb" not in capsys.readouterr().err
