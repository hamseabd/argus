"""The seeded-bug corpus is well formed: every expected bug sits on a line the diff shows."""

from pathlib import Path

import pytest

from argus.context.diff import parse_diff
from argus.context.git import local_context, local_diff
from evals.corpus import CASES_DIR, Case, build_case_repo, load_cases

CASES = load_cases()
SEEDED_BUGS = {
    "sqli",
    "off_by_one",
    "path_traversal",
    "missing_await",
    "leaked_file_handle",
    "none_deref",
    "removed_auth_check",
}
CLEAN_CONTROLS = {"clean_rename_refactor", "clean_docs_only", "clean_correct_fix"}


def test_the_corpus_holds_seven_seeded_bugs_and_three_clean_controls() -> None:
    names = {case.name for case in CASES}

    assert names == SEEDED_BUGS | CLEAN_CONTROLS
    assert {case.name for case in CASES if case.clean} == CLEAN_CONTROLS
    assert [case.name for case in CASES] == sorted(names)  # stable order for reports


def test_every_case_directory_has_base_seeded_and_expected() -> None:
    for path in CASES_DIR.iterdir():
        if path.is_dir():
            assert (path / "base").is_dir(), path.name
            assert (path / "seeded").is_dir(), path.name
            assert (path / "expected.yaml").is_file(), path.name


def test_each_seeded_case_expects_exactly_one_bug_and_each_control_none() -> None:
    for case in CASES:
        assert len(case.expected) == (0 if case.clean else 1), case.name


def test_load_cases_rejects_an_unknown_category(tmp_path: Path) -> None:
    case = tmp_path / "bad"
    (case / "base").mkdir(parents=True)
    (case / "seeded").mkdir()
    (case / "expected.yaml").write_text(
        "- {file: a.py, line_start: 1, line_end: 1, category: style, severity: low}\n"
    )

    with pytest.raises(ValueError, match="category"):
        load_cases(tmp_path)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_the_feature_branch_changes_something_and_every_expected_line_is_in_the_diff(
    case: Case, tmp_path: Path
) -> None:
    repo = build_case_repo(case, tmp_path / case.name)

    context = local_context(repo, base="main")
    shown = {f.path: f.commentable_lines for f in parse_diff(local_diff(repo, "main"))}

    assert context.files, "the seeded change must not be empty"
    for bug in case.expected:
        lines = set(range(bug.line_start, bug.line_end + 1))
        assert lines <= shown.get(bug.file, frozenset()), (case.name, bug)
