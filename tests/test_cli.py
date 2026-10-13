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


def test_feedback_reads_reactions_and_prints_the_table(monkeypatch, tmp_path) -> None:
    from typer.testing import CliRunner

    from argus import cli

    asked: dict = {}

    class FakeGitHub:
        def __init__(self, token: str) -> None:
            asked["token"] = token

        def repo_review_comments(self, owner, repo, since=None):
            asked["repo"] = (owner, repo, since)
            return [
                {
                    "id": 1,
                    "pull_request_url": "https://api.github.com/repos/o/r/pulls/7",
                    "html_url": "https://github.com/o/r/pull/7#discussion_r1",
                    "path": "a.py",
                    "line": 1,
                    "user": {"login": "argus[bot]", "type": "Bot"},
                    "body": "**[HIGH] Leak** · correctness · confirmed · confidence 0.90",
                    "reactions": {"+1": 1, "-1": 0},
                }
            ]

    monkeypatch.setattr(cli, "GitHubClient", FakeGitHub)
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
    monkeypatch.setenv("ARGUS_REVIEWER_LOGIN", "argus[bot]")
    out = tmp_path / "feedback.json"

    result = CliRunner().invoke(
        cli.app,
        ["feedback", "--repo", "o/r", "--since", "2026-09-01", "--json", str(out)],
    )

    assert result.exit_code == 0, result.output
    assert asked["repo"] == ("o", "r", "2026-09-01T00:00:00Z")
    assert "| correctness | 1 | 0 | 0 | 100% |" in result.output
    assert '"thumbs_up": 1' in out.read_text()


def test_feedback_needs_the_identity_argus_posts_as(monkeypatch) -> None:
    from typer.testing import CliRunner

    from argus import cli

    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
    monkeypatch.delenv("ARGUS_REVIEWER_LOGIN", raising=False)

    result = CliRunner().invoke(cli.app, ["feedback", "--repo", "o/r"])

    assert result.exit_code == 1
    assert "ARGUS_REVIEWER_LOGIN" in result.output
