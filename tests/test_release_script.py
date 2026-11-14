"""scripts/release.sh on throwaway repositories: what reaches the public one, what never does."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "release.sh"
PRIVATE_NAME = "secret-sibling"
REAL_GIT = shutil.which("git") or "git"


def isolated(base: dict[str, str]) -> dict[str, str]:
    """The host's environment without its git settings, so no GIT_* variable or config leaks in."""
    return {
        **{key: value for key, value in base.items() if not key.startswith("GIT_")},
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }


GIT_ENV = isolated(dict(os.environ))


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [REAL_GIT, *args], cwd=cwd, env=GIT_ENV, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit_file(repo: Path, path: str, text: str, message: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    git(repo, "add", "-f", path)
    git(repo, "commit", "-q", "-m", message)


def push_to_origin(dev: Path, path: str, text: str) -> None:
    commit_file(dev, path, text, f"add {path}")
    git(dev, "push", "-q", "origin", "main")


def fake(bin_dir: Path, name: str, script: str) -> None:
    tool = bin_dir / name
    tool.write_text(f"#!/bin/sh\n{script}\n")
    tool.chmod(0o755)


def fake_gh(bin_dir: Path, listing: str, login: str = "hamseabd") -> None:
    """A gh logged in as `login` whose private-repository listing is the output of `listing`."""
    fake(bin_dir, "gh", f'if [ "$1" = api ]; then echo {login}; else {listing}; fi')


def names(*repos: str) -> str:
    return f"printf '%s\\n' {' '.join(repos)}"


def fake_git(bin_dir: Path, before: str) -> None:
    """A git that runs the shell snippet `before` first, then the real git."""
    fake(bin_dir, "git", f'{before}\nexec {REAL_GIT} "$@"')


@pytest.fixture
def dev(tmp_path: Path) -> Path:
    """A dev checkout whose origin holds two commits on main, a tracked docs/ among them."""
    git(tmp_path, "init", "-q", "--bare", "-b", "main", "origin.git")
    git(tmp_path, "init", "-q", "--bare", "-b", "main", "public.git")
    repo = tmp_path / "dev"
    git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), "dev")
    git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    commit_file(repo, "README.md", "# Argus\n", "first")
    commit_file(repo, "docs/notes.md", f"working notes about {PRIVATE_NAME}\n", "notes")
    git(repo, "push", "-q", "origin", "main")
    return repo


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    """Tools first on the script's PATH: a gh that lists argus-dev and one other private repo."""
    path = tmp_path / "bin"
    path.mkdir()
    fake_gh(path, names("argus-dev", PRIVATE_NAME))
    return path


@pytest.fixture
def env(tmp_path: Path, bin_dir: Path) -> dict[str, str]:
    """The script's environment: isolated git, the fake tools, and a local public remote."""
    return {
        **GIT_ENV,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "ARGUS_RELEASE_REMOTE": str(tmp_path / "public.git"),
    }


def release(dev: Path, env: dict[str, str], version: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), version], cwd=dev, env=env, capture_output=True, text=True, check=False
    )


def released_sha(result: subprocess.CompletedProcess[str]) -> str:
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


def public(dev: Path) -> Path:
    return dev.parent / "public.git"


def published_files(dev: Path) -> list[str]:
    return git(public(dev), "ls-tree", "-r", "--name-only", "main").splitlines()


def assert_nothing_published(dev: Path) -> None:
    assert git(public(dev), "for-each-ref") == ""


def test_first_release_is_one_parentless_commit_tagged_and_printed(
    dev: Path, env: dict[str, str]
) -> None:
    sha = released_sha(release(dev, env, "v1"))

    source = git(dev, "rev-parse", "--short", "origin/main")
    assert git(public(dev), "rev-parse", "main") == sha
    assert git(public(dev), "rev-parse", "v1^{commit}") == sha
    assert git(public(dev), "rev-list", "--count", "main") == "1"
    subject = git(public(dev), "log", "-1", "--format=%s", "main")
    assert subject == f"release: v1 from argus-dev {source}"


def test_release_tree_is_main_without_docs(dev: Path, env: dict[str, str]) -> None:
    released_sha(release(dev, env, "v1"))

    assert published_files(dev) == ["README.md"]


def test_local_changes_to_docs_never_block_the_release(dev: Path, env: dict[str, str]) -> None:
    commit_file(dev, "docs/notes.md", "rewritten locally\n", "local notes")
    (dev / "docs" / "notes.md").write_text("edited, not committed\n")

    released_sha(release(dev, env, "v1"))

    assert published_files(dev) == ["README.md"]


def test_next_release_sits_on_the_last_release_never_on_dev_history(
    dev: Path, env: dict[str, str]
) -> None:
    first = released_sha(release(dev, env, "v1"))
    push_to_origin(dev, "argus.py", "VERSION = 2\n")

    second = released_sha(release(dev, env, "v2"))

    assert git(public(dev), "rev-parse", f"{second}^") == first
    assert git(public(dev), "rev-list", "--count", "main") == "2"


def test_a_public_main_that_is_not_a_release_is_never_buried(
    dev: Path, env: dict[str, str], tmp_path: Path
) -> None:
    released_sha(release(dev, env, "v1"))
    git(tmp_path, "clone", "-q", str(public(dev)), "public-clone")
    commit_file(tmp_path / "public-clone", "HOTFIX.md", "made on the public repo\n", "hotfix")
    git(tmp_path / "public-clone", "push", "-q", "origin", "main")
    hotfix = git(public(dev), "rev-parse", "main")
    push_to_origin(dev, "argus.py", "VERSION = 2\n")

    result = release(dev, env, "v2")

    assert result.returncode != 0
    assert "is not a tagged release" in result.stderr
    assert git(public(dev), "rev-parse", "main") == hotfix
    assert "refs/tags/v2" not in git(public(dev), "for-each-ref", "--format=%(refname)")


def test_an_existing_tag_is_refused_and_nothing_moves(dev: Path, env: dict[str, str]) -> None:
    first = released_sha(release(dev, env, "v1"))
    push_to_origin(dev, "argus.py", "VERSION = 2\n")

    again = release(dev, env, "v1")

    assert again.returncode != 0
    assert "v1 already exists" in again.stderr
    assert git(public(dev), "rev-parse", "main") == first


def test_origin_main_is_fetched_so_a_stale_local_copy_is_never_released(
    dev: Path, env: dict[str, str], tmp_path: Path
) -> None:
    git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), "other")
    commit_file(tmp_path / "other", "newer.py", "X = 2\n", "pushed from elsewhere")
    git(tmp_path / "other", "push", "-q", "origin", "main")

    released_sha(release(dev, env, "v1"))

    assert "newer.py" in published_files(dev)


def test_a_local_commit_not_on_origin_main_is_never_released(
    dev: Path, env: dict[str, str]
) -> None:
    commit_file(dev, "unpushed.py", "X = 1\n", "local only")

    released_sha(release(dev, env, "v1"))

    assert "unpushed.py" not in published_files(dev)


def test_a_tree_naming_a_private_repository_is_refused_before_any_push(
    dev: Path, env: dict[str, str]
) -> None:
    push_to_origin(dev, "README.md", f"# Argus\n\nSee {PRIVATE_NAME.upper()}.\n")

    result = release(dev, env, "v1")

    assert result.returncode != 0
    assert f"README.md:See {PRIVATE_NAME.upper()}." in result.stderr
    assert_nothing_published(dev)


@pytest.mark.parametrize("form", ["secret_sibling.app", "Secret Sibling", "SecretSibling"])
def test_another_spelling_of_a_private_name_is_refused(
    dev: Path, env: dict[str, str], form: str
) -> None:
    push_to_origin(dev, "config.py", f"MODULE = '{form}'\n")

    result = release(dev, env, "v1")

    assert result.returncode != 0
    assert f"config.py:MODULE = '{form}'" in result.stderr
    assert_nothing_published(dev)


def test_a_private_name_inside_a_binary_file_is_refused(dev: Path, env: dict[str, str]) -> None:
    image = dev / "shot.png"
    image.write_bytes(b"\x89PNG\x00\x00tEXt\x00" + PRIVATE_NAME.encode() + b"\x00")
    git(dev, "add", "shot.png")
    git(dev, "commit", "-q", "-m", "screenshot")
    git(dev, "push", "-q", "origin", "main")

    result = release(dev, env, "v1")

    assert result.returncode != 0
    assert "shot.png" in result.stderr
    assert_nothing_published(dev)


@pytest.mark.parametrize(
    "path", ["evals/cases/secret_sibling_pr5/expected.yaml", "Evals/SECRET-SIBLING/notes.txt"]
)
def test_a_private_name_in_a_file_path_is_refused(
    dev: Path, env: dict[str, str], path: str
) -> None:
    push_to_origin(dev, path, "kind: bug\n")

    result = release(dev, env, "v1")

    assert result.returncode != 0
    assert f"path: {path}" in result.stderr
    assert_nothing_published(dev)


@pytest.mark.parametrize("text", ["secret-siblings", "topsecret-sibling", "secret-sibling2"])
def test_a_private_name_inside_a_longer_word_is_allowed(
    dev: Path, env: dict[str, str], text: str
) -> None:
    push_to_origin(dev, f"{text}.md", f"About {text}.\n")

    released_sha(release(dev, env, "v1"))


@pytest.mark.parametrize("form", ["one_two three", "one-two-three", "OneTwoThree"])
def test_every_separator_of_a_many_word_private_name_is_read_loosely(
    dev: Path, env: dict[str, str], bin_dir: Path, form: str
) -> None:
    fake_gh(bin_dir, names("argus-dev", "one-two_three"))
    push_to_origin(dev, "README.md", f"# Argus\n\nSee {form}.\n")

    result = release(dev, env, "v1")

    assert result.returncode != 0
    assert "names a private repository" in result.stderr


def test_a_trailing_dash_in_a_private_name_stays_part_of_it(
    dev: Path, env: dict[str, str], bin_dir: Path
) -> None:
    fake_gh(bin_dir, names("argus-dev", "Notes-"))
    push_to_origin(dev, "README.md", "# Argus\n\nWorking notes live elsewhere.\n")

    released_sha(release(dev, env, "v1"))
    push_to_origin(dev, "README.md", "# Argus\n\nSee notes-.\n")

    result = release(dev, env, "v2")

    assert result.returncode != 0
    assert "README.md:See notes-." in result.stderr
    assert "names a private repository" in result.stderr


def test_the_dev_repository_own_name_is_allowed(dev: Path, env: dict[str, str]) -> None:
    push_to_origin(dev, "README.md", "# Argus\n\nDeveloped in argus-dev.\n")

    released_sha(release(dev, env, "v1"))


@pytest.mark.parametrize(
    ("gh", "message"),
    [
        ("echo 'gh: not logged in' >&2; exit 1", "could not read the gh login"),
        ('if [ "$1" = api ]; then echo hamseabd; else exit 1; fi', "could not list"),
    ],
)
def test_a_failing_gh_aborts_the_release_before_any_push(
    dev: Path, env: dict[str, str], bin_dir: Path, gh: str, message: str
) -> None:
    fake(bin_dir, "gh", gh)

    result = release(dev, env, "v1")

    assert result.returncode != 0
    assert message in result.stderr
    assert_nothing_published(dev)


def test_gh_logged_in_as_anyone_but_the_owner_aborts_the_release(
    dev: Path, env: dict[str, str], bin_dir: Path
) -> None:
    fake_gh(bin_dir, names("argus-dev", PRIVATE_NAME), login="a-collaborator")

    result = release(dev, env, "v1")

    assert result.returncode != 0
    assert "logged in as a-collaborator, not hamseabd" in result.stderr
    assert_nothing_published(dev)


@pytest.mark.parametrize(
    ("listing", "message"),
    [
        ("exit 0", "did not list argus-dev"),
        (names("argus-dev"), "listed no private repository but argus-dev"),
        ("printf 'argus-dev\\n'; seq -f 'repo%g' 999", "reached its limit of 1000"),
    ],
)
def test_a_gh_listing_that_cannot_be_complete_aborts_the_release(
    dev: Path, env: dict[str, str], bin_dir: Path, listing: str, message: str
) -> None:
    fake_gh(bin_dir, listing)

    result = release(dev, env, "v1")

    assert result.returncode != 0
    assert message in result.stderr
    assert_nothing_published(dev)


@pytest.mark.parametrize(
    "broken",
    [
        'if [ "$1" = grep ]; then echo "fatal: scan broke" >&2; exit 128; fi',
        'case "$*" in *core.quotePath=false*) echo "fatal: scan broke" >&2; exit 128;; esac',
    ],
    ids=["content scan", "path scan"],
)
def test_a_failing_scan_aborts_the_release_before_any_push(
    dev: Path, env: dict[str, str], bin_dir: Path, broken: str
) -> None:
    fake_git(bin_dir, broken)

    result = release(dev, env, "v1")

    assert result.returncode != 0
    assert "fatal: scan broke" in result.stderr
    assert "scan failed" in result.stderr
    assert_nothing_published(dev)


def test_a_public_main_that_moves_mid_release_keeps_its_commit_and_gets_no_tag(
    dev: Path, env: dict[str, str], bin_dir: Path, tmp_path: Path
) -> None:
    released_sha(release(dev, env, "v1"))
    interloper = tmp_path / "interloper"
    git(tmp_path, "clone", "-q", str(public(dev)), "interloper")
    push_to_origin(dev, "argus.py", "VERSION = 2\n")
    fake_git(
        bin_dir,
        f'if [ "$1" = commit-tree ]; then (cd {interloper} && {REAL_GIT} commit -q --allow-empty '
        f"-m interloper && {REAL_GIT} push -q origin main) >&2; fi",
    )

    result = release(dev, env, "v2")

    assert result.returncode != 0
    assert git(public(dev), "rev-parse", "main") == git(interloper, "rev-parse", "HEAD")
    assert "refs/tags/v2" not in git(public(dev), "for-each-ref", "--format=%(refname)")


def test_a_release_away_from_canary_warns_that_it_has_not_soaked(
    dev: Path, env: dict[str, str]
) -> None:
    git(dev, "tag", "canary", "origin/main~1")
    git(dev, "push", "-q", "origin", "canary")

    result = release(dev, env, "v1")

    released_sha(result)
    assert "warning: canary is at" in result.stderr


def test_a_release_at_canary_says_nothing_about_it(dev: Path, env: dict[str, str]) -> None:
    git(dev, "tag", "canary", "origin/main")
    git(dev, "push", "-q", "origin", "canary")

    result = release(dev, env, "v1")

    released_sha(result)
    assert result.stderr == ""


@pytest.mark.parametrize("version", ["v1", "v10", "v1.2", "v1.2.3"])
def test_a_well_formed_version_is_released(dev: Path, env: dict[str, str], version: str) -> None:
    sha = released_sha(release(dev, env, version))

    assert git(public(dev), "rev-parse", f"{version}^{{commit}}") == sha


@pytest.mark.parametrize(
    "version", ["1", "v", "V1", "v1 ", "v1.", "v1.2.3.4", "latest", "v1.x", ""]
)
def test_a_malformed_version_is_refused(dev: Path, env: dict[str, str], version: str) -> None:
    result = release(dev, env, version)

    assert result.returncode != 0
    assert_nothing_published(dev)
