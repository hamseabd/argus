"""The reviewers read only the repository under review.

A prompt injection in a diff, a PR body, or a symlink the PR adds can ask a
reviewer to read a file outside the repository: the process environment,
the CLI's credentials, the runner's home. Whatever it reads can end up in a
finding, and a finding is posted on the pull request. So every read tool's
path is resolved, symlinks included, and refused unless it stays inside the
repository and out of its .git directory.
"""

import asyncio
from pathlib import Path

import pytest

from argus.agent.hooks import HookState, build_hooks, confine_reads


def call(state: HookState, root: Path, tool: str, tool_input: dict) -> dict:
    data = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input}
    return asyncio.run(confine_reads(state, root)(data, None, {"signal": None}))


def denied(out: dict) -> bool:
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "app" / "main.py").write_text("x = 1\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[http]\n\textraheader = AUTHORIZATION: basic x\n")
    (tmp_path / "secret.txt").write_text("sk-ant-oat01-secret\n")
    return root


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Read", {"file_path": "app/main.py"}),
        ("Read", {"file_path": "{root}/app/main.py"}),
        ("Grep", {"pattern": "x"}),
        ("Grep", {"pattern": "x", "path": "app"}),
        ("Glob", {"pattern": "**/*.py"}),
        ("Glob", {"pattern": "*.py", "path": "{root}/app"}),
    ],
)
def test_reads_inside_the_repository_pass(repo: Path, tool: str, tool_input: dict) -> None:
    resolved = {k: v.replace("{root}", str(repo)) for k, v in tool_input.items()}

    assert call(HookState(), repo, tool, resolved) == {}


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Read", {"file_path": "/proc/self/environ"}),
        ("Read", {"file_path": "../secret.txt"}),
        ("Read", {"file_path": "{root}/../secret.txt"}),
        ("Read", {"file_path": "~/.claude/.credentials.json"}),
        ("Read", {"file_path": ".git/config"}),
        ("Grep", {"pattern": "sk-ant", "path": "/"}),
        ("Grep", {"pattern": "x", "path": ".git"}),
        ("Glob", {"pattern": "*", "path": "/etc"}),
        ("Glob", {"pattern": "/home/**/.credentials.json"}),
        ("Glob", {"pattern": "../**"}),
        ("Glob", {"pattern": ".git/**"}),
        ("Glob", {"pattern": "**/.git/config"}),
        ("Glob", {"pattern": "{..,x}/**"}),
        ("Glob", {"pattern": ".gi[t]/**"}),
        ("Grep", {"pattern": "token", "glob": ".gi?/config"}),
        ("Grep", {"pattern": "token", "glob": "?git/config"}),
        ("Grep", {"pattern": "token", "glob": ".git/config"}),
        ("Grep", {"pattern": "x", "glob": "../*"}),
        ("Read", {"file_path": "~nosuchuser-argus/x"}),
    ],
)
def test_reads_outside_the_repository_are_denied(repo: Path, tool: str, tool_input: dict) -> None:
    state = HookState()
    resolved = {k: v.replace("{root}", str(repo)) for k, v in tool_input.items()}

    out = call(state, repo, tool, resolved)

    assert denied(out)
    assert "outside the repository" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert state.denied == 1


def test_a_symlink_the_change_adds_cannot_point_a_read_outside(repo: Path) -> None:
    (repo / "app" / "notes.md").symlink_to(repo.parent / "secret.txt")

    assert denied(call(HookState(), repo, "Read", {"file_path": "app/notes.md"}))


def test_a_symlinked_directory_cannot_point_a_search_outside(repo: Path) -> None:
    (repo / "docs").symlink_to(repo.parent)

    assert denied(call(HookState(), repo, "Grep", {"pattern": "sk-ant", "path": "docs"}))


def test_a_read_with_no_path_is_denied_rather_than_guessed(repo: Path) -> None:
    assert denied(call(HookState(), repo, "Read", {}))


def test_other_tools_are_not_this_hooks_business(repo: Path) -> None:
    assert call(HookState(), repo, "Agent", {"prompt": "/proc/self/environ"}) == {}


def test_the_confinement_hook_runs_on_every_read_tool(repo: Path) -> None:
    matchers = build_hooks(HookState(), repo)["PreToolUse"]

    confining = next(m for m in matchers if "confine_reads" in m.hooks[0].__qualname__)
    assert set((confining.matcher or "").split("|")) == {"Read", "Grep", "Glob"}


def test_a_search_whose_scope_holds_a_symlink_out_of_the_repository_is_refused(
    repo: Path,
) -> None:
    # A glob can walk into a linked directory by pattern alone ("docs/**"), so
    # any search whose scope holds such a link is refused, whatever its pattern.
    (repo / "docs").symlink_to(repo.parent)

    for tool, tool_input in [
        ("Glob", {"pattern": "docs/**"}),
        ("Grep", {"pattern": "sk-ant"}),
        ("Grep", {"pattern": "x", "path": "."}),
    ]:
        out = call(HookState(), repo, tool, tool_input)
        assert denied(out), (tool, tool_input)
        assert "docs" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_search_scoped_away_from_the_escaping_symlink_still_runs(repo: Path) -> None:
    (repo / "docs").symlink_to(repo.parent)

    assert call(HookState(), repo, "Grep", {"pattern": "x", "path": "app"}) == {}
    assert call(HookState(), repo, "Read", {"file_path": "app/main.py"}) == {}


def test_a_symlink_that_stays_inside_the_repository_does_not_block_searches(repo: Path) -> None:
    (repo / "alias").symlink_to(repo / "app")

    assert call(HookState(), repo, "Glob", {"pattern": "**/*.py"}) == {}
