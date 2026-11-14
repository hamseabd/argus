"""The seeded-bug corpus is well formed: every expected bug sits on a line the diff shows."""

from pathlib import Path

import pytest

from argus.context.diff import parse_diff
from argus.context.git import local_context, local_diff
from evals.corpus import CASES_DIR, Case, CaseBuildError, build_case_repo, load_cases

CASES = load_cases()
ORIGINAL_BUGS = {
    "sqli",
    "off_by_one",
    "path_traversal",
    "missing_await",
    "leaked_file_handle",
    "none_deref",
    "removed_auth_check",
    "cross_file_contract",
    "js_async_foreach",
    "truncated_diff_bug",
}
"""Correctness and security: the seven v1 cases, the baseline every later run is held to,
and the cross-file, JavaScript, and truncated-diff cases that followed."""
V2_BUGS = {
    "tests_tautological_assert",
    "tests_mocked_unit",
    "tests_missing_negative_case",
    "maintainability_duplicated_logic",
    "maintainability_misleading_comment",
    "maintainability_docstring_drift",
    "design_n_plus_one",
    "design_unbounded_growth",
    "design_layering_violation",
    "conventions_print_not_logger",
    "conventions_bare_exception",
}
"""One case or more for each specialist v2 added."""
SEEDED_BUGS = ORIGINAL_BUGS | V2_BUGS
CLEAN_CONTROLS = {
    "clean_rename_refactor",
    "clean_docs_only",
    "clean_correct_fix",
    "clean_tested_feature",
    "clean_named_constant",
    "clean_added_log_context",
}
VERIFIER_TRAPS = {
    "trap_subprocess_list_args",
    "trap_inclusive_range",
    "trap_except_reraise",
    "trap_validated_path",
}
"""Correct changes that look like a classic defect: the verifier should reject the claim."""
FROM_REAL_FINDINGS = {"argus_dev_pr55_c4255102517"}
"""Proposed from a finding Argus posted on a real pull request, added on the maintainer's word."""


def test_every_v2_case_is_filed_under_the_specialist_its_name_starts_with() -> None:
    for case in CASES:
        if case.name in V2_BUGS:
            assert [bug.category for bug in case.expected] == [case.name.split("_")[0]]


def test_the_conventions_cases_carry_their_rules_on_the_base_branch() -> None:
    for case in CASES:
        if case.name.startswith("conventions_"):
            assert any((case.path / "base" / f).is_file() for f in ("REVIEW.md", "CLAUDE.md"))


def test_the_corpus_holds_seeded_bugs_clean_controls_and_verifier_traps() -> None:
    names = {case.name for case in CASES}

    assert names == SEEDED_BUGS | CLEAN_CONTROLS | VERIFIER_TRAPS | FROM_REAL_FINDINGS
    assert {case.name for case in CASES if case.clean} == CLEAN_CONTROLS
    assert {case.name for case in CASES if case.kind == "verifier-trap"} == VERIFIER_TRAPS
    assert [case.name for case in CASES] == sorted(names)  # stable order for reports


def test_every_case_directory_has_base_seeded_and_expected() -> None:
    for path in CASES_DIR.iterdir():
        if path.is_dir():
            assert (path / "base").is_dir(), path.name
            assert (path / "seeded").is_dir(), path.name
            assert (path / "expected.yaml").is_file(), path.name


def test_each_seeded_case_expects_exactly_one_bug_and_the_others_none() -> None:
    for case in CASES:
        assert len(case.expected) == (1 if case.kind == "bug" else 0), case.name


def test_load_cases_rejects_an_unknown_category(tmp_path: Path) -> None:
    case = tmp_path / "bad"
    (case / "base").mkdir(parents=True)
    (case / "seeded").mkdir()
    (case / "expected.yaml").write_text(
        "- {file: a.py, line_start: 1, line_end: 1, category: style, severity: low}\n"
    )

    with pytest.raises(ValueError, match="category"):
        load_cases(tmp_path)


def test_a_git_failure_while_building_a_case_is_a_case_build_error(tmp_path: Path) -> None:
    source = tmp_path / "unchanged"
    for side in ("base", "seeded"):
        (source / side).mkdir(parents=True)
        (source / side / "a.py").write_text("x = 1\n")
    case = Case(name="unchanged", path=source, expected=())

    with pytest.raises(CaseBuildError, match="nothing to commit"):
        build_case_repo(case, tmp_path / "repo")


def test_a_case_with_no_snapshot_on_disk_is_a_case_build_error(tmp_path: Path) -> None:
    case = Case(name="missing", path=tmp_path / "missing", expected=())

    with pytest.raises(CaseBuildError, match="missing"):
        build_case_repo(case, tmp_path / "repo")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_the_feature_branch_changes_something_and_every_expected_line_is_in_the_diff(
    case: Case, tmp_path: Path
) -> None:
    repo = build_case_repo(case, tmp_path / case.name)

    context = local_context(repo, base="main")
    shown = {f.path: f.commentable_lines for f in parse_diff(local_diff(repo, "main"))}

    assert context.files, "the seeded change must not be empty"
    for target in (*case.expected, *case.traps):
        lines = set(range(target.line_start, target.line_end + 1))
        assert lines <= shown.get(target.file, frozenset()), (case.name, target)


def test_alternate_locations_are_parsed_and_need_not_be_in_the_diff() -> None:
    (case,) = [c for c in CASES if c.name == "cross_file_contract"]
    (bug,) = case.expected

    assert bug.file == "app/accounts.py"
    assert [(loc.file, loc.line_start) for loc in bug.also] == [("app/billing.py", 9)]


def test_the_truncation_case_drops_its_buggy_file_from_the_diff_under_its_cap(
    tmp_path: Path,
) -> None:
    (case,) = [c for c in CASES if c.name == "truncated_diff_bug"]
    repo = build_case_repo(case, tmp_path / "repo")

    capped = local_context(repo, base="main", max_bytes=case.diff_size_cap)

    assert case.expected[0].file in capped.truncated_files
    assert capped.diff_text.strip(), "something must still be in the diff"


def test_a_case_yaml_with_an_unknown_key_is_rejected(tmp_path: Path) -> None:
    case = tmp_path / "clean_bad"
    (case / "base").mkdir(parents=True)
    (case / "seeded").mkdir()
    (case / "expected.yaml").write_text("[]\n")
    (case / "case.yaml").write_text("diff_cap: 10\n")

    with pytest.raises(ValueError, match="diff_cap"):
        load_cases(tmp_path)


def _case_dir(root: Path, name: str, expected: str) -> Path:
    case = root / name
    (case / "base").mkdir(parents=True)
    (case / "seeded").mkdir()
    (case / "expected.yaml").write_text(expected)
    return case


def test_a_bare_list_is_a_bug_case_and_a_clean_prefix_with_none_is_a_control(
    tmp_path: Path,
) -> None:
    _case_dir(
        tmp_path,
        "sqli",
        "- {file: a.py, line_start: 1, line_end: 1, category: security, severity: high}\n",
    )
    _case_dir(tmp_path, "clean_docs", "[]\n")

    clean, bug = load_cases(tmp_path)  # sorted by name

    assert (bug.kind, clean.kind) == ("bug", "clean")
    assert clean.clean and not bug.clean


def test_a_bug_is_owned_by_the_specialist_of_its_category_unless_named(tmp_path: Path) -> None:
    _case_dir(
        tmp_path,
        "pair",
        "kind: bug\nbugs:\n"
        "  - {file: a.py, line_start: 1, line_end: 1, category: security, severity: high}\n"
        "  - {file: a.py, line_start: 5, line_end: 5, category: tests, severity: low,"
        " specialist: correctness}\n",
    )

    (case,) = load_cases(tmp_path)

    assert [bug.owner for bug in case.expected] == ["security", "correctness"]


def test_a_verifier_trap_lists_the_plausible_non_bug_and_expects_no_bug(tmp_path: Path) -> None:
    _case_dir(
        tmp_path,
        "trap_shell",
        "kind: verifier-trap\ntraps:\n"
        "  - {file: a.py, line_start: 3, line_end: 4, category: security}\n",
    )

    (case,) = load_cases(tmp_path)

    assert case.kind == "verifier-trap"
    assert case.expected == ()
    assert [(t.file, t.line_start, t.category) for t in case.traps] == [("a.py", 3, "security")]
    assert not case.clean


@pytest.mark.parametrize(
    ("expected", "problem"),
    [
        (
            "kind: clean\nbugs:\n  - {file: a.py, line_start: 1, line_end: 1, category: tests,"
            " severity: low}\n",
            "clean",
        ),
        ("kind: bug\n", "bug"),
        ("kind: verifier-trap\n", "trap"),
        (
            "kind: verifier-trap\ntraps:\n  - {file: a.py, line_start: 1, line_end: 1,"
            " category: tests}\nbugs:\n  - {file: a.py, line_start: 1, line_end: 1,"
            " category: tests, severity: low}\n",
            "trap",
        ),
        ("kind: flaky\n", "kind"),
    ],
)
def test_an_expected_yaml_whose_kind_and_contents_disagree_is_rejected(
    tmp_path: Path, expected: str, problem: str
) -> None:
    _case_dir(tmp_path, "bad", expected)

    with pytest.raises(ValueError, match=problem):
        load_cases(tmp_path)
