import subprocess
from pathlib import Path

import pytest

from argus.context.git import head_sha, increment_since, local_context, local_diff, repo_root
from argus.domain.errors import GitError
from argus.domain.models import ChangedFile


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo on branch feature, one commit ahead of main, with a dirty working tree."""
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "argus@test")
    git(tmp_path, "config", "user.name", "argus")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "module.py").write_text("".join(f"line{i}\n" for i in range(1, 11)))
    (pkg / "gone.py").write_text("old\n")
    (pkg / "old_name.py").write_text("".join(f"value {i}\n" for i in range(1, 31)))
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    git(tmp_path, "checkout", "-q", "-b", "feature")
    (pkg / "module.py").write_text(
        "".join(f"line{i}\n" for i in range(1, 11)).replace("line3", "three")
    )
    (pkg / "gone.py").unlink()
    git(tmp_path, "mv", "pkg/old_name.py", "pkg/new_name.py")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "committed change")
    (pkg / "new.py").write_text("uncommitted\n")
    git(tmp_path, "add", "pkg/new.py")  # staged but not committed
    (pkg / "module.py").write_text((pkg / "module.py").read_text() + "unstaged tail\n")
    return tmp_path


def test_repo_root_finds_the_toplevel_from_a_subdirectory(repo: Path) -> None:
    assert repo_root(repo / "pkg") == repo.resolve()


def test_repo_root_outside_a_repository_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(GitError):
        repo_root(tmp_path)


def test_head_sha_is_forty_hex_chars(repo: Path) -> None:
    sha = head_sha(repo)

    assert len(sha) == 40
    assert sha == git(repo, "rev-parse", "HEAD")


def test_local_diff_covers_commits_since_the_merge_base_and_the_working_tree(repo: Path) -> None:
    text = local_diff(repo, base="main")

    assert "+three" in text  # committed on feature
    assert "+uncommitted" in text  # staged
    assert "+unstaged tail" in text  # unstaged
    assert "rename from pkg/old_name.py" in text  # rename detection is on


def test_local_diff_ignores_commits_on_the_base_after_branching(repo: Path) -> None:
    git(repo, "stash", "-q", "--include-untracked")
    git(repo, "checkout", "-q", "main")
    (repo / "only_on_main.py").write_text("x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "main moved on")
    git(repo, "checkout", "-q", "feature")

    text = local_diff(repo, base="main")

    assert "only_on_main" not in text
    assert "+three" in text


def test_local_diff_with_unknown_base_is_an_error(repo: Path) -> None:
    with pytest.raises(GitError, match="nope"):
        local_diff(repo, base="nope")


def test_local_diff_is_empty_when_nothing_changed(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "argus@test")
    git(tmp_path, "config", "user.name", "argus")
    (tmp_path / "a.py").write_text("a\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")

    assert local_diff(tmp_path, base="main") == ""


def test_local_context_lists_changed_files_in_diff_order(repo: Path) -> None:
    ctx = local_context(repo / "pkg", base="main")

    assert ctx.source == "local"
    assert ctx.pr is None
    assert ctx.repo_root == repo.resolve()
    assert ctx.diff_text == local_diff(repo, base="main")
    assert ctx.truncated_files == []
    assert ctx.files == [
        ChangedFile(path="pkg/gone.py", status="removed"),
        ChangedFile(path="pkg/module.py", status="modified"),
        ChangedFile(path="pkg/new.py", status="added"),
        ChangedFile(path="pkg/new_name.py", status="renamed", previous_path="pkg/old_name.py"),
    ]


def test_local_context_applies_the_size_cap_but_still_lists_every_file(repo: Path) -> None:
    full = local_context(repo, base="main")

    capped = local_context(repo, base="main", max_bytes=len(full.diff_text.encode()) // 2)

    assert capped.truncated_files
    assert len(capped.diff_text.encode()) <= len(full.diff_text.encode()) // 2
    assert [f.path for f in capped.files] == [f.path for f in full.files]
    for path in capped.truncated_files:
        assert f"+++ b/{path}" not in capped.diff_text


def test_local_diff_output_is_independent_of_user_diff_config(repo: Path) -> None:
    git(repo, "config", "diff.noprefix", "true")
    git(repo, "config", "diff.mnemonicPrefix", "true")
    (repo / "img.bin").write_bytes(b"\x00\x01")
    git(repo, "add", "img.bin")

    ctx = local_context(repo, base="main")

    assert [f.path for f in ctx.files] == [
        "img.bin",
        "pkg/gone.py",
        "pkg/module.py",
        "pkg/new.py",
        "pkg/new_name.py",
    ]


def test_non_ascii_paths_are_not_quoted(repo: Path) -> None:
    git(repo, "config", "core.quotePath", "true")
    (repo / "café.py").write_text("x\n")
    git(repo, "add", "café.py")

    ctx = local_context(repo, base="main")

    assert "café.py" in [f.path for f in ctx.files]


def test_non_utf8_content_does_not_escape_as_a_decode_error(repo: Path) -> None:
    (repo / "latin1.txt").write_bytes("caf\xe9\n".encode("latin-1"))
    git(repo, "add", "latin1.txt")

    ctx = local_context(repo, base="main")

    assert "latin1.txt" in [f.path for f in ctx.files]


def test_option_like_base_is_rejected_not_interpreted(repo: Path) -> None:
    with pytest.raises(GitError):
        local_diff(repo, base="--help")


def test_local_context_drops_ignored_files_before_the_cap_and_lists_them(repo: Path) -> None:
    (repo / "uv.lock").write_text("version = 1\n" * 2000)
    git(repo, "add", "uv.lock")
    full = local_context(repo, base="main")

    ctx = local_context(repo, base="main", ignore=["*.lock", "pkg/gone.py"])

    assert ctx.ignored_files == ["pkg/gone.py", "uv.lock"]
    assert [f.path for f in ctx.files] == ["pkg/module.py", "pkg/new.py", "pkg/new_name.py"]
    assert "uv.lock" not in ctx.diff_text
    assert "pkg/gone.py" not in ctx.diff_text
    assert ctx.truncated_files == []
    assert "uv.lock" in full.diff_text


def test_an_ignored_file_does_not_count_against_the_size_cap(repo: Path) -> None:
    (repo / "uv.lock").write_text("version = 1\n" * 2000)
    git(repo, "add", "uv.lock")
    small = local_context(repo, base="main", ignore=["*.lock"])

    capped = local_context(
        repo, base="main", max_bytes=len(small.diff_text.encode()), ignore=["*.lock"]
    )

    assert capped.truncated_files == []
    assert capped.ignored_files == ["uv.lock"]


@pytest.fixture
def pushed(tmp_path: Path) -> tuple[Path, str, str]:
    """A branch reviewed at one commit, then pushed to again: (repo, reviewed sha, new head)."""
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "argus@test")
    git(tmp_path, "config", "user.name", "argus")
    (tmp_path / "a.py").write_text("one\n")
    (tmp_path / "b.py").write_text("two\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    git(tmp_path, "checkout", "-q", "-b", "feature")
    (tmp_path / "a.py").write_text("one, reviewed\n")
    git(tmp_path, "commit", "-qam", "first push")
    reviewed = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "b.py").write_text("two, after the review\n")
    (tmp_path / "poetry.lock").write_text("pinned\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "second push")
    return tmp_path, reviewed, git(tmp_path, "rev-parse", "HEAD")


def test_the_increment_is_only_what_changed_since_the_reviewed_commit(pushed) -> None:
    repo, reviewed, head = pushed

    increment = increment_since(repo, reviewed, head, ignore=("*.lock",))

    assert increment is not None
    assert increment.since_sha == reviewed
    assert [f.path for f in increment.files] == ["b.py"]
    assert "after the review" in increment.diff_text
    assert "a.py" not in increment.diff_text


def test_after_a_force_push_there_is_no_increment(pushed) -> None:
    repo, _, head = pushed
    git(repo, "checkout", "-q", "-b", "rewritten", "main")
    (repo / "c.py").write_text("elsewhere\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "unrelated")
    unrelated = git(repo, "rev-parse", "HEAD")

    assert increment_since(repo, unrelated, head) is None


def test_an_unknown_reviewed_commit_has_no_increment(pushed) -> None:
    repo, _, head = pushed

    assert increment_since(repo, "f" * 40, head) is None


def test_the_increment_respects_the_size_cap(pushed) -> None:
    repo, reviewed, head = pushed

    increment = increment_since(repo, reviewed, head, max_bytes=10)

    assert increment is not None
    assert increment.truncated_files


def test_a_merge_since_the_review_means_no_increment(pushed) -> None:
    # Merging the base into the branch brings in code the pull request did not
    # write; diffing from the reviewed commit would review all of it.
    repo, reviewed, _ = pushed
    git(repo, "checkout", "-q", "main")
    (repo / "upstream.py").write_text("landed on main\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "elsewhere on main")
    git(repo, "checkout", "-q", "feature")
    git(repo, "merge", "-q", "--no-edit", "main")
    head = git(repo, "rev-parse", "HEAD")

    assert increment_since(repo, reviewed, head) is None
