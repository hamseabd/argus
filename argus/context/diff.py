"""Unified diff parsing and the commentable-line index.

A GitHub review comment can only be attached to a line that appears in the
pull request diff, and only on the new side of it. This module turns a
git-style unified diff into one FileDiff per file, each carrying the set of
new-side line numbers that a hunk shows as added or context. That set is
what decides whether a finding becomes an inline comment or a note in the
review body.

The parser keeps each file's raw section verbatim so the diff can be
reassembled after the size cap drops whole files.
"""

import re
from dataclasses import dataclass

from argus.domain.errors import DiffParseError
from argus.domain.models import ChangedFile, ChangedFileStatus

DIFF_SIZE_CAP = 200 * 1024
"""Bytes of diff text the lead reviewer is given at most; larger files are dropped."""

_FILE_HEADER = "diff --git "
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,\d+)? @@")
_NO_PATH = "/dev/null"
_C_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}


@dataclass(frozen=True)
class FileDiff:
    """One file's section of a unified diff."""

    path: str
    """Repository-relative path on the new side; the old path for a removed file."""
    previous_path: str | None
    status: ChangedFileStatus
    text: str
    """The section verbatim, from its diff --git line to the next file's."""
    commentable_lines: frozenset[int]
    """New-side line numbers shown in a hunk as added or context."""
    size_bytes: int
    """UTF-8 size of text, computed once because the size cap sorts and sums on it."""

    def to_changed_file(self) -> ChangedFile:
        return ChangedFile(path=self.path, status=self.status, previous_path=self.previous_path)


def parse_diff(text: str) -> list[FileDiff]:
    """Split a unified diff into files, in the order they appear."""
    return [_parse_section(section) for section in _split_sections(text)]


def commentable_index(files: list[FileDiff]) -> dict[str, frozenset[int]]:
    """Map each path to the new-side lines a review comment may attach to."""
    return {f.path: f.commentable_lines for f in files}


def cap_diff(files: list[FileDiff], max_bytes: int = DIFF_SIZE_CAP) -> tuple[str, list[str]]:
    """Reassemble the diff under a byte budget, dropping the largest files first.

    Returns the kept text, in the original file order, and the paths that were
    dropped, also in the original order so the list reads like the diff did.
    """
    total = sum(f.size_bytes for f in files)
    dropped: set[int] = set()
    for index in sorted(range(len(files)), key=lambda i: files[i].size_bytes, reverse=True):
        if total <= max_bytes:
            break
        dropped.add(index)
        total -= files[index].size_bytes
    kept = "".join(f.text for i, f in enumerate(files) if i not in dropped)
    truncated = [f.path for i, f in enumerate(files) if i in dropped]
    return kept, truncated


def _split_lines(text: str) -> list[str]:
    """Split on newline only; str.splitlines would also cut on form feeds and U+2028."""
    return text.split("\n")


def _split_sections(text: str) -> list[str]:
    """Cut the text at each "diff --git" line; anything before the first is not a file."""
    sections: list[str] = []
    current: list[str] = []
    for line in re.split(r"(?<=\n)", text):
        if not line:
            continue
        if line.startswith(_FILE_HEADER):
            if current:
                sections.append("".join(current))
            current = []
        elif not current:
            continue
        current.append(line)
    if current:
        sections.append("".join(current))
    return sections


def _parse_section(section: str) -> FileDiff:
    lines = _split_lines(section.removesuffix("\n"))
    old_path, new_path = _header_paths(lines[0])
    status: ChangedFileStatus = "modified"
    previous_path: str | None = None
    hunks_start: int | None = None
    for i, line in enumerate(lines[1:], start=1):
        if line.startswith("new file mode"):
            status = "added"
        elif line.startswith("deleted file mode"):
            status = "removed"
        elif line.startswith("rename from "):
            status = "renamed"
            previous_path = _unquote(line.removeprefix("rename from "))
        elif line.startswith("rename to "):
            new_path = _unquote(line.removeprefix("rename to "))
        elif line.startswith("--- "):
            old_path = _strip_marker(line, "--- ")
        elif line.startswith("+++ "):
            new_path = _strip_marker(line, "+++ ")
        elif line.startswith("@@"):
            hunks_start = i
            break
    path = old_path if status == "removed" or new_path is None else new_path
    if path is None:
        raise DiffParseError(f"cannot determine the file path from {lines[0]!r}")
    if status == "renamed" and previous_path is None:
        previous_path = old_path
    commentable = _commentable_lines(lines[hunks_start:]) if hunks_start is not None else set()
    return FileDiff(
        path=path,
        previous_path=previous_path,
        status=status,
        text=section,
        commentable_lines=frozenset(commentable),
        size_bytes=len(section.encode()),
    )


def _header_paths(header: str) -> tuple[str | None, str | None]:
    """Recover the paths from a "diff --git a/X b/Y" line.

    Paths may contain spaces, so the line is ambiguous in general; the
    unambiguous "---", "+++", and "rename" lines override these when present.
    Binary and pure-rename sections have no "---"/"+++" lines, so this
    fallback must still get the common cases right.
    """
    rest = header.removeprefix(_FILE_HEADER)
    if rest.startswith('"'):
        return _quoted_header_paths(rest)
    for i in range(len(rest)):
        if rest.startswith(" b/", i):
            old, new = rest[:i], rest[i + 1 :]
            if old.startswith("a/") and old[2:] == new[2:]:
                return old[2:], new[2:]
    split = rest.find(" b/")
    if split == -1:
        return None, None
    return rest[:split].removeprefix("a/"), rest[split + 1 :].removeprefix("b/")


def _quoted_header_paths(rest: str) -> tuple[str | None, str | None]:
    """Paths git had to quote ("a/x" "b/y") are unambiguous: the quotes delimit them."""
    parts: list[str] = []
    i = 0
    while i < len(rest) and rest[i] == '"':
        end = i + 1
        while end < len(rest) and rest[end] != '"':
            end += 2 if rest[end] == "\\" else 1
        parts.append(_unquote(rest[i : end + 1]))
        i = end + 2  # past the closing quote and the separating space
    if len(parts) != 2:
        return None, None
    return parts[0].removeprefix("a/"), parts[1].removeprefix("b/")


def _unquote(value: str) -> str:
    """Decode a C-style quoted path as git writes it under core.quotePath."""
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        return value
    out = bytearray()
    body = value[1:-1]
    i = 0
    while i < len(body):
        char = body[i]
        if char != "\\":
            out += char.encode()
            i += 1
            continue
        nxt = body[i + 1] if i + 1 < len(body) else ""
        octal = body[i + 1 : i + 4]
        if len(octal) == 3 and all(c in "01234567" for c in octal):
            out.append(int(octal, 8) & 0xFF)
            i += 4
        else:
            out += _C_ESCAPES.get(nxt, nxt).encode()
            i += 2
    return out.decode("utf-8", errors="replace")


def _strip_marker(line: str, marker: str) -> str | None:
    """Turn "--- a/path" or "+++ b/path\\t" into "path"; /dev/null becomes None."""
    value = _unquote(line.removeprefix(marker).rstrip("\t"))
    if value == _NO_PATH:
        return None
    if value.startswith(("a/", "b/")):
        value = value[2:]
    return value


def _commentable_lines(hunk_lines: list[str]) -> set[int]:
    lines: set[int] = set()
    new_line = 0
    for line in hunk_lines:
        if line.startswith("@@"):
            match = _HUNK_HEADER.match(line)
            if match is None:
                raise DiffParseError(f"malformed hunk header: {line!r}")
            new_line = int(match.group("start"))
            continue
        if line.startswith("\\"):
            continue  # "\ No newline at end of file" annotates the previous line
        if line.startswith("-"):
            continue
        # "+" is an added line; " " or an empty string is context (some tools
        # strip the trailing space from a blank context line).
        lines.add(new_line)
        new_line += 1
    return lines
