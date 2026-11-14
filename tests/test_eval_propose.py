"""`python -m evals.propose`: a real finding snapshotted into a corpus case, never committed."""

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import evals.propose
from argus import cli
from argus.domain.errors import GitHubError
from argus.report.github_review import HANDOFF_PREFIX, REVIEW_MARKER
from evals.corpus import load_cases
from evals.propose import ProposeError, parse_target, propose_case

BASE = "b" * 40
HEAD = "h" * 40
MERGE_BASE = "m" * 40


class FakeGitHub:
    def __init__(self, *, up: int = 1, down: int = 0, files: list[dict] | None = None) -> None:
        self.comment = {
            "id": 99,
            "pull_request_review_id": 50,
            "pull_request_url": "https://api.github.com/repos/o/r/pulls/7",
            "html_url": "https://github.com/o/r/pull/7#discussion_r99",
            "path": "app/store.py",
            "original_commit_id": HEAD,
            "original_start_line": 3,
            "original_line": 4,
            "line": None,
            "body": "**[HIGH] Lost update** · correctness · confirmed · confidence 0.90\n\nD.",
            "reactions": {"+1": up, "-1": down},
            "user": {"login": "argus[bot]", "type": "Bot"},
        }
        self.files = files or [
            {"filename": "app/store.py", "status": "modified"},
            {"filename": "app/new.py", "status": "added"},
            {"filename": "app/gone.py", "status": "removed"},
            {"filename": "app/moved.py", "status": "renamed", "previous_filename": "app/old.py"},
        ]
        self.read: list[tuple[str, str]] = []
        self.compared: list[tuple[str, str]] = []
        self.pr_paths = {f["filename"] for f in self.files} | {"app/old.py"}

    def review_comment(self, owner: str, repo: str, comment_id: int) -> dict:
        assert (owner, repo, comment_id) == ("o", "r", 99)
        return self.comment

    def pr_info(self, owner: str, repo: str, number: int):
        from argus.domain.models import PRInfo

        return PRInfo(
            owner=owner,
            repo=repo,
            number=number,
            title="t",
            body="",
            base_ref="main",
            head_ref="f",
            head_sha="z" * 40,
            html_url="u",
            base_sha=BASE,
        )

    def compare(self, owner: str, repo: str, base: str, head: str) -> dict:
        self.compared.append((base, head))
        return {"merge_base_commit": {"sha": MERGE_BASE}, "files": self.files}

    def pr_files(self, owner: str, repo: str, number: int) -> list:
        from argus.domain.models import ChangedFile

        return [ChangedFile(path=p, status="modified") for p in sorted(self.pr_paths)]

    def file_text(self, owner: str, repo: str, path: str, ref: str) -> str:
        self.read.append((path, ref))
        return f"{path} at {ref[0]}\n"

    def pr_reviews(self, owner: str, repo: str, number: int) -> list[dict]:
        handoff = json.dumps(
            {"version": 2, "findings": [{"id": "c-1", "specialist": "design", "comment": 99}]}
        )
        return [
            {
                "id": 50,
                "user": {"login": "argus[bot]", "type": "Bot"},
                "body": f"{REVIEW_MARKER}\n\nS.\n{HANDOFF_PREFIX}{handoff} -->",
            }
        ]


def propose(tmp_path: Path, github: FakeGitHub) -> Path:
    return propose_case(github, "o", "r", 7, 99, reviewer_login="argus[bot]", cases_dir=tmp_path)


def test_a_true_finding_becomes_a_bug_case_at_the_head_argus_reviewed(tmp_path: Path) -> None:
    github = FakeGitHub()

    case = propose(tmp_path, github)

    assert github.compared == [(BASE, HEAD)]
    assert case == tmp_path / "r_pr7_c99"
    assert (case / "base/app/store.py").read_text() == "app/store.py at m\n"
    assert (case / "seeded/app/store.py").read_text() == "app/store.py at h\n"
    assert (case / "seeded/app/new.py").exists() and not (case / "base/app/new.py").exists()
    assert (case / "base/app/gone.py").exists() and not (case / "seeded/app/gone.py").exists()
    assert (case / "base/app/old.py").exists() and (case / "seeded/app/moved.py").exists()
    expected = yaml.safe_load((case / "expected.yaml").read_text())
    assert expected == {
        "kind": "bug",
        "bugs": [
            {
                "file": "app/store.py",
                "line_start": 3,
                "line_end": 4,
                "category": "correctness",
                "severity": "high",
                "specialist": "design",
            }
        ],
    }
    text = (case / "expected.yaml").read_text()
    assert "https://github.com/o/r/pull/7#discussion_r99" in text
    assert "Lost update" in text


def test_a_thumbs_down_finding_becomes_a_clean_control(tmp_path: Path) -> None:
    case = propose(tmp_path, FakeGitHub(up=0, down=1))

    assert case.name == "clean_r_pr7_c99"
    assert yaml.safe_load((case / "expected.yaml").read_text()) == {"kind": "clean"}


def test_a_proposed_case_loads_as_part_of_a_corpus(tmp_path: Path) -> None:
    propose(tmp_path, FakeGitHub())

    (case,) = load_cases(tmp_path)

    assert case.kind == "bug"
    assert case.expected[0].owner == "design"


def test_a_comment_that_is_not_an_argus_finding_is_refused(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.comment["body"] = "Looks good to me."

    with pytest.raises(ProposeError, match="not an Argus finding"):
        propose(tmp_path, github)


def test_a_comment_from_another_pull_request_is_refused(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.comment["pull_request_url"] = "https://api.github.com/repos/o/r/pulls/8"

    with pytest.raises(ProposeError, match="pull request 8"):
        propose(tmp_path, github)


def test_an_existing_case_is_never_overwritten(tmp_path: Path) -> None:
    propose(tmp_path, FakeGitHub())

    with pytest.raises(ProposeError, match="already exists"):
        propose(tmp_path, FakeGitHub())


def test_a_path_that_escapes_the_case_directory_is_refused(tmp_path: Path) -> None:
    github = FakeGitHub(files=[{"filename": "../../outside.py", "status": "modified"}])

    with pytest.raises(ProposeError, match="outside"):
        propose(tmp_path / "cases", github)

    assert not (tmp_path / "outside.py").exists()
    assert not (tmp_path / "cases" / "r_pr7_c99").exists()


@pytest.mark.parametrize(
    ("target", "parsed"),
    [("o/r#7", ("o", "r", 7)), ("hamseabd/argus-dev#123", ("hamseabd", "argus-dev", 123))],
)
def test_the_target_is_owner_name_hash_number(target: str, parsed: tuple) -> None:
    assert parse_target(target) == parsed


@pytest.mark.parametrize("target", ["o/r", "o/r#x", "r#7", "o/r#0"])
def test_a_malformed_target_is_refused(target: str) -> None:
    with pytest.raises(ProposeError):
        parse_target(target)


def test_the_command_writes_the_case_and_says_it_is_not_committed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(evals.propose, "GitHubClient", lambda token: FakeGitHub())
    monkeypatch.setattr(evals.propose, "CASES_DIR", tmp_path / "cases")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
    monkeypatch.setenv("ARGUS_REVIEWER_LOGIN", "argus[bot]")

    result = CliRunner().invoke(evals.propose.app, ["o/r#7", "99"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "cases" / "r_pr7_c99" / "expected.yaml").exists()
    assert "not committed" in result.output


def test_the_command_reports_a_github_failure_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Failing(FakeGitHub):
        def review_comment(self, owner: str, repo: str, comment_id: int) -> dict:
            raise GitHubError(404, "Not Found")

    monkeypatch.setattr(evals.propose, "GitHubClient", lambda token: Failing())
    monkeypatch.setattr(evals.propose, "CASES_DIR", tmp_path / "cases")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
    monkeypatch.setenv("ARGUS_REVIEWER_LOGIN", "argus[bot]")

    result = CliRunner().invoke(evals.propose.app, ["o/r#7", "99"])

    assert result.exit_code == 1
    assert "404" in result.output


def test_the_installed_argus_cli_has_no_eval_command() -> None:
    result = CliRunner().invoke(cli.app, ["eval", "propose", "o/r#7", "99"])

    assert result.exit_code == 2


def test_a_comment_by_anyone_but_argus_is_refused(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.comment["user"] = {"login": "mallory", "type": "User"}

    with pytest.raises(ProposeError, match="not an Argus finding"):
        propose(tmp_path, github)


def test_without_the_argus_login_nothing_is_trusted(tmp_path: Path) -> None:
    with pytest.raises(ProposeError, match="ARGUS_REVIEWER_LOGIN"):
        propose_case(FakeGitHub(), "o", "r", 7, 99, reviewer_login="", cases_dir=tmp_path)


def test_a_failure_part_way_leaves_no_case_behind(tmp_path: Path) -> None:
    class Flaky(FakeGitHub):
        def file_text(self, owner: str, repo: str, path: str, ref: str) -> str:
            if path == "app/gone.py":
                raise GitHubError(502, "bad gateway")
            return super().file_text(owner, repo, path, ref)

    cases = tmp_path / "cases"

    with pytest.raises(GitHubError):
        propose(cases, Flaky())

    assert not (cases / "r_pr7_c99").exists()
    assert [p.name for p in tmp_path.iterdir()] == ["cases"]  # no staging left either
    assert propose(cases, FakeGitHub()) == cases / "r_pr7_c99"  # and a retry works


def test_without_a_handoff_record_the_agent_the_comment_names_owns_the_bug(
    tmp_path: Path,
) -> None:
    github = FakeGitHub()
    github.comment["body"] = github.comment["body"].replace(
        "confidence 0.90", "confidence 0.90 · found by security"
    )
    github.comment["id"] = 98  # the handoff names comment 99 only

    case = propose_case(github, "o", "r", 7, 99, reviewer_login="argus[bot]", cases_dir=tmp_path)

    assert (
        yaml.safe_load((case / "expected.yaml").read_text())["bugs"][0]["specialist"] == "security"
    )


def test_only_the_files_the_pull_request_changes_are_snapshotted(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.files.append({"filename": "app/elsewhere.py", "status": "modified"})

    case = propose(tmp_path, github)

    assert not (case / "seeded/app/elsewhere.py").exists()
    assert not (case / "base/app/elsewhere.py").exists()


def test_a_given_base_replaces_the_pull_requests_base(tmp_path: Path) -> None:
    github = FakeGitHub()

    propose_case(
        github, "o", "r", 7, 99, reviewer_login="argus[bot]", base="s" * 40, cases_dir=tmp_path
    )

    assert github.compared == [("s" * 40, HEAD)]
