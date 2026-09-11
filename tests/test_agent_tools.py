import asyncio
import subprocess
from pathlib import Path

import pytest

from argus.agent.tools import GIT_HISTORY_TOOL_NAME, UNCOMMITTED, build_argus_server, git_history

CALC_V1 = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
CALC_V2 = CALC_V1.replace("a - b", "b - a")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "argus@test")
    git(tmp_path, "config", "user.name", "Argus Tester")
    src = tmp_path / "src"
    src.mkdir()
    (src / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
    )
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "feat: add calculator")
    (src / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return b - a\n"
    )
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "fix: swap operands in sub")
    (tmp_path / "untracked.py").write_text("x = 1\n")
    return tmp_path


def test_history_for_a_changed_range_lists_newest_first(repo: Path) -> None:
    commits = git_history(repo, "src/calc.py", 5, 6)

    assert [c["subject"] for c in commits] == ["fix: swap operands in sub", "feat: add calculator"]
    assert all(len(c["sha"]) == 40 for c in commits)
    assert commits[0]["author"] == "Argus Tester"
    assert commits[0]["date"].startswith("20")


def test_history_for_an_untouched_range_has_only_the_creating_commit(repo: Path) -> None:
    commits = git_history(repo, "src/calc.py", 1, 2)

    assert [c["subject"] for c in commits] == ["feat: add calculator"]


def test_untracked_file_has_no_history(repo: Path) -> None:
    assert git_history(repo, "untracked.py", 1, 1) == []


def test_missing_file_has_no_history(repo: Path) -> None:
    assert git_history(repo, "nope.py", 1, 1) == []


def test_path_outside_the_repository_is_rejected(repo: Path) -> None:
    with pytest.raises(ValueError, match="outside"):
        git_history(repo, "../etc/passwd", 1, 1)
    with pytest.raises(ValueError, match="outside"):
        git_history(repo, "/etc/passwd", 1, 1)


def test_bad_line_range_is_rejected(repo: Path) -> None:
    with pytest.raises(ValueError):
        git_history(repo, "src/calc.py", 0, 1)
    with pytest.raises(ValueError):
        git_history(repo, "src/calc.py", 3, 2)


def test_range_past_the_end_of_the_file_is_reported(repo: Path) -> None:
    with pytest.raises(ValueError, match="lines"):
        git_history(repo, "src/calc.py", 1, 999)


def test_server_exposes_one_read_only_tool(repo: Path) -> None:
    server = build_argus_server(repo)

    assert server["type"] == "sdk"
    assert server["name"] == "argus"
    assert GIT_HISTORY_TOOL_NAME == "mcp__argus__git_history"


def test_tool_handler_returns_json_text(repo: Path) -> None:
    from argus.agent.tools import git_history_tool

    tool = git_history_tool(repo)
    result = asyncio.run(tool.handler({"path": "src/calc.py", "start_line": 5, "end_line": 6}))

    assert result.get("is_error") is not True
    text = result["content"][0]["text"]
    assert "swap operands" in text
    assert text.lstrip().startswith("[")


def test_tool_handler_reports_errors_instead_of_raising(repo: Path) -> None:
    from argus.agent.tools import git_history_tool

    tool = git_history_tool(repo)
    result = asyncio.run(tool.handler({"path": "../x", "start_line": 1, "end_line": 1}))

    assert result["is_error"] is True
    assert "outside" in result["content"][0]["text"]


def test_uncommitted_lines_are_labelled_not_misattributed(repo: Path) -> None:
    calc = repo / "src" / "calc.py"
    calc.write_text("import os\nimport sys\nimport re\n" + calc.read_text())  # 3 new lines on top

    assert git_history(repo, "src/calc.py", 1, 2) == [dict(UNCOMMITTED)]


def test_committed_lines_below_an_uncommitted_edit_are_mapped_to_head(repo: Path) -> None:
    calc = repo / "src" / "calc.py"
    calc.write_text("import os\nimport sys\nimport re\n" + calc.read_text())

    commits = git_history(repo, "src/calc.py", 8, 9)  # sub's body, lines 5-6 in HEAD

    assert [c["subject"] for c in commits] == ["fix: swap operands in sub", "feat: add calculator"]


def test_a_range_spanning_uncommitted_and_committed_lines_reports_both(repo: Path) -> None:
    calc = repo / "src" / "calc.py"
    calc.write_text("import os\nimport sys\nimport re\n" + calc.read_text())

    entries = git_history(repo, "src/calc.py", 3, 4)  # line 3 new, line 4 is HEAD line 1

    assert entries[0] == dict(UNCOMMITTED)
    assert [e["subject"] for e in entries[1:]] == ["feat: add calculator"]


def test_a_staged_new_file_has_no_history_yet(repo: Path) -> None:
    (repo / "fresh.py").write_text("a\nb\n")
    git(repo, "add", "fresh.py")

    assert git_history(repo, "fresh.py", 1, 2) == [dict(UNCOMMITTED)]


def test_uncommitted_deletion_above_the_range_shifts_the_mapping(repo: Path) -> None:
    calc = repo / "src" / "calc.py"
    calc.write_text("".join(calc.read_text().splitlines(keepends=True)[3:]))  # drop add()

    commits = git_history(repo, "src/calc.py", 2, 3)  # sub's body, lines 5-6 in HEAD

    assert [c["subject"] for c in commits] == ["fix: swap operands in sub", "feat: add calculator"]
