from pathlib import Path

import pytest

from argus.context.diff import DIFF_SIZE_CAP, FileDiff, cap_diff, commentable_index, parse_diff
from argus.domain.errors import ArgusError, DiffParseError
from argus.domain.models import ChangedFile

FIXTURES = Path(__file__).parent / "fixtures" / "diffs"


def load(name: str) -> str:
    return (FIXTURES / name).read_text()


def by_path(files: list[FileDiff]) -> dict[str, FileDiff]:
    return {f.path: f for f in files}


def test_empty_input_has_no_files() -> None:
    assert parse_diff("") == []
    assert parse_diff("\n") == []


def test_modified_file_indexes_added_and_context_lines_on_the_new_side() -> None:
    (file,) = parse_diff(load("modified.diff"))

    assert file.path == "pkg/module.py"
    assert file.previous_path is None
    assert file.status == "modified"
    # Hunk 1 covers new lines 1-9 (one replaced, one inserted); hunk 2 covers 16-21.
    assert file.commentable_lines == frozenset(range(1, 10)) | frozenset(range(16, 22))


def test_removed_lines_do_not_shift_the_new_side_numbering() -> None:
    (file,) = parse_diff(load("modified.diff"))

    # "-line3" is followed by "+line3 changed" at new line 3; nothing is numbered 10-15.
    assert 3 in file.commentable_lines
    assert not any(n in file.commentable_lines for n in range(10, 16))


def test_added_removed_and_renamed_files() -> None:
    files = by_path(parse_diff(load("added_removed_renamed.diff")))

    assert files["pkg/new.py"].status == "added"
    assert files["pkg/new.py"].commentable_lines == frozenset({1, 2})

    assert files["pkg/gone.py"].status == "removed"
    assert files["pkg/gone.py"].commentable_lines == frozenset()

    assert files["pkg/new_name.py"].status == "renamed"
    assert files["pkg/new_name.py"].previous_path == "pkg/old_name.py"
    assert files["pkg/new_name.py"].commentable_lines == frozenset()


def test_rename_with_an_edit_and_a_space_in_the_path() -> None:
    (file,) = parse_diff(load("renamed_with_edit.diff"))

    assert file.path == "pkg/spaced name.py"
    assert file.previous_path == "pkg/big.py"
    assert file.status == "renamed"
    assert file.commentable_lines == frozenset(range(4, 11))


def test_binary_file_is_listed_without_commentable_lines() -> None:
    (file,) = parse_diff(load("binary.diff"))

    assert file.path == "img.bin"
    assert file.status == "modified"
    assert file.commentable_lines == frozenset()


def test_no_newline_marker_is_not_a_line() -> None:
    (file,) = parse_diff(load("no_newline.diff"))

    assert file.commentable_lines == frozenset({1})


def test_mixed_diff_keeps_file_order_and_reassembles_verbatim() -> None:
    text = load("mixed.diff")

    files = parse_diff(text)

    assert [f.path for f in files] == [
        "img.bin",
        "nonl.txt",
        "pkg/gone.py",
        "pkg/module.py",
        "pkg/new.py",
        "pkg/new_name.py",
    ]
    assert "".join(f.text for f in files) == text
    assert sum(f.size_bytes for f in files) == len(text.encode())


def test_text_before_the_first_file_header_is_ignored() -> None:
    text = " x.py | 1 +\n 1 file changed\n\n" + load("modified.diff")

    files = parse_diff(text)

    assert [f.path for f in files] == ["pkg/module.py"]
    assert files[0].text == load("modified.diff")


def test_malformed_hunk_header_is_an_error() -> None:
    text = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ nonsense @@\n+a\n"

    with pytest.raises(DiffParseError, match="hunk header"):
        parse_diff(text)


def test_blank_hunk_line_counts_as_context() -> None:
    # Some tools strip the trailing space from an empty context line.
    text = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n a\n\n+c\n"

    (file,) = parse_diff(text)

    assert file.commentable_lines == frozenset({1, 2, 3})


def test_hunk_header_without_counts_means_one_line() -> None:
    text = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"

    (file,) = parse_diff(text)

    assert file.commentable_lines == frozenset({1})


def test_to_changed_file_carries_status_and_previous_path() -> None:
    files = by_path(parse_diff(load("renamed_with_edit.diff")))

    assert files["pkg/spaced name.py"].to_changed_file() == ChangedFile(
        path="pkg/spaced name.py", status="renamed", previous_path="pkg/big.py"
    )


def test_commentable_index_maps_path_to_lines() -> None:
    index = commentable_index(parse_diff(load("mixed.diff")))

    assert index["pkg/new.py"] == frozenset({1, 2})
    assert index["img.bin"] == frozenset()
    assert set(index) == {
        "img.bin",
        "nonl.txt",
        "pkg/gone.py",
        "pkg/module.py",
        "pkg/new.py",
        "pkg/new_name.py",
    }


def test_cap_keeps_everything_when_it_fits() -> None:
    text = load("mixed.diff")
    files = parse_diff(text)

    kept, truncated = cap_diff(files, max_bytes=len(text.encode()))

    assert kept == text
    assert truncated == []


def test_cap_drops_the_largest_files_first_and_keeps_order() -> None:
    text = load("mixed.diff")
    files = parse_diff(text)
    largest = max(files, key=lambda f: f.size_bytes)
    assert largest.path == "pkg/module.py"

    kept, truncated = cap_diff(files, max_bytes=len(text.encode()) - 1)

    assert truncated == ["pkg/module.py"]
    assert kept == "".join(f.text for f in files if f.path != "pkg/module.py")


def test_cap_can_drop_several_files_and_lists_them_in_diff_order() -> None:
    files = parse_diff(load("mixed.diff"))
    two_largest = sorted(files, key=lambda f: f.size_bytes, reverse=True)[:2]
    budget = sum(f.size_bytes for f in files) - sum(f.size_bytes for f in two_largest)

    kept, truncated = cap_diff(files, max_bytes=budget)

    assert truncated == [f.path for f in files if f in two_largest]
    assert kept == "".join(f.text for f in files if f not in two_largest)
    assert len(kept.encode()) <= budget


def test_cap_of_zero_drops_everything() -> None:
    files = parse_diff(load("mixed.diff"))

    kept, truncated = cap_diff(files, max_bytes=0)

    assert kept == ""
    assert truncated == [f.path for f in files]


def test_default_cap_is_200_kib() -> None:
    assert DIFF_SIZE_CAP == 200 * 1024


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("*.diff")))
def test_every_fixture_parses_and_reassembles(name: str) -> None:
    text = load(name)

    files = parse_diff(text)

    assert files
    assert "".join(f.text for f in files) == text


def test_form_feed_inside_a_line_does_not_split_it() -> None:
    text = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,4 @@\n a\n+b\x0cc\n d\n e\n"
    )

    (file,) = parse_diff(text)

    assert file.commentable_lines == frozenset({1, 2, 3, 4})


def test_line_separator_characters_do_not_start_a_new_file() -> None:
    text = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n"
        "+z\u2028diff --git a/evil b/evil\n"
    )

    files = parse_diff(text)

    assert [f.path for f in files] == ["x.py"]


def test_quoted_paths_are_unescaped() -> None:
    text = (
        'diff --git "a/caf\\303\\251 \\"q\\".py" "b/caf\\303\\251 \\"q\\".py"\n'
        'index 1..2 100644\n--- "a/caf\\303\\251 \\"q\\".py"\n+++ "b/caf\\303\\251 \\"q\\".py"\n'
        "@@ -1 +1 @@\n-a\n+b\n"
    )

    (file,) = parse_diff(text)

    assert file.path == 'café "q".py'
    assert file.commentable_lines == frozenset({1})


def test_quoted_path_on_a_binary_file_without_marker_lines() -> None:
    text = (
        'diff --git "a/bin \\303\\251.bin" "b/bin \\303\\251.bin"\n'
        "index 1..2 100644\nBinary files a/bin é.bin and b/bin é.bin differ\n"
    )

    (file,) = parse_diff(text)

    assert file.path == "bin é.bin"


def test_malformed_quoted_path_does_not_crash() -> None:
    text = 'diff --git "a/x\\" "b/x\\"\nBinary files differ\n'

    (file,) = parse_diff(text)

    assert file.path == "x\\"


def test_parse_errors_are_argus_errors() -> None:
    assert issubclass(DiffParseError, ArgusError)
    with pytest.raises(DiffParseError):
        parse_diff("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@@ merge @@@\n+a\n")
    with pytest.raises(DiffParseError):
        parse_diff("diff --git nonsense\nBinary files differ\n")


def test_size_bytes_is_computed_once() -> None:
    (file,) = parse_diff(load("modified.diff"))

    assert file.size_bytes == len(file.text.encode())
    assert "size_bytes" in file.__dataclass_fields__
