from pathlib import Path

import pytest

from argus.context.source import read_source, within_repository


def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "app" / "db.py").write_text("import sqlite3\n\nconn = sqlite3.connect(':memory:')\n")
    return root


def test_a_file_inside_the_repository_is_read_whole(tmp_path: Path) -> None:
    root = repo(tmp_path)

    assert read_source(root, "app/db.py") == (root / "app" / "db.py").read_text()


def test_a_path_that_climbs_out_of_the_repository_is_not_read(tmp_path: Path) -> None:
    root = repo(tmp_path)
    (tmp_path / "secret.txt").write_text("token")

    assert read_source(root, "../secret.txt") is None
    assert read_source(root, str(tmp_path / "secret.txt")) is None


def test_a_symlink_that_points_out_of_the_repository_is_not_followed(tmp_path: Path) -> None:
    root = repo(tmp_path)
    (tmp_path / "secret.txt").write_text("token")
    (root / "link.py").symlink_to(tmp_path / "secret.txt")

    assert read_source(root, "link.py") is None


def test_a_missing_file_or_a_directory_reads_as_none(tmp_path: Path) -> None:
    root = repo(tmp_path)

    assert read_source(root, "app/gone.py") is None
    assert read_source(root, "app") is None


def test_a_file_over_the_size_cap_is_not_read(tmp_path: Path) -> None:
    root = repo(tmp_path)

    assert read_source(root, "app/db.py", max_bytes=10) is None


def test_nothing_under_git_is_read(tmp_path: Path) -> None:
    root = repo(tmp_path)
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[http]\n\textraheader = AUTHORIZATION: basic x\n")

    assert read_source(root, ".git/config") is None


@pytest.mark.parametrize("git", [".git", ".GIT", ".Git"])
def test_git_in_any_case_is_outside_the_repository(git: str) -> None:
    """Pure, so it holds on a case-sensitive filesystem where .GIT does not exist."""
    root = Path("/repo")

    assert within_repository(root, root / "app" / "db.py")
    assert within_repository(root, root / ".github" / "workflows" / "ci.yml")
    assert within_repository(root, root / ".gitignore")
    assert not within_repository(root, root / git / "config")
    assert not within_repository(root, root / "vendor" / git / "config")
    assert not within_repository(root, Path("/elsewhere/db.py"))
