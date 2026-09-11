"""The one custom tool Argus gives its reviewers: read-only git history.

Reviewers and the verifier need to tell a regression from an intentional
change, and that is a question for git log. They do not get Bash, so this
in-process MCP server exposes exactly one query: which commits touched a
line range of a file. It never runs anything but read-only git commands,
and it refuses paths outside the repository root.

Line numbers are those of the file as it is now in the working tree, the
same numbering the diff and every finding use. In local mode that file may
carry uncommitted edits, so the range is translated to HEAD's numbering
through the uncommitted hunks first, and lines that are not committed yet
are reported as such rather than attributed to an older commit.
"""

import asyncio
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    McpSdkServerConfig,
    SdkMcpTool,
    ToolAnnotations,
    create_sdk_mcp_server,
    tool,
)

from argus import __version__

SERVER_NAME = "argus"
TOOL_NAME = "git_history"
GIT_HISTORY_TOOL_NAME = f"mcp__{SERVER_NAME}__{TOOL_NAME}"
MAX_COMMITS = 20
UNCOMMITTED: dict[str, str] = {
    "sha": "",
    "date": "",
    "author": "",
    "subject": "uncommitted change in the working tree (no history yet)",
}
_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

Hunk = tuple[int, int, int, int]
"""(old_start, old_len, new_start, new_len) of one uncommitted hunk."""


def git_history(repo_root: Path, path: str, start_line: int, end_line: int) -> list[dict[str, str]]:
    """Commits that touched lines start_line..end_line of path, newest first.

    Returns an empty list for a file git does not track. If the range covers
    lines that are not committed yet, the first entry is UNCOMMITTED and the
    rest is the history of the committed part of the range, if any. Raises
    ValueError for a path outside the repository or a range past the end of
    the file.
    """
    if start_line < 1 or end_line < start_line:
        raise ValueError(f"invalid line range {start_line}-{end_line}; need 1 <= start <= end")
    root = repo_root.resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"{path} is outside the repository")
    relative = target.relative_to(root).as_posix()
    if _run(root, "ls-files", "--error-unmatch", "--", relative).returncode != 0:
        return []
    hunks = _uncommitted_hunks(root, relative)
    entries: list[dict[str, str]] = []
    if any(_touches(hunk, start_line, end_line) for hunk in hunks):
        entries.append(dict(UNCOMMITTED))
    head_start, head_end = _to_head_range(hunks, start_line, end_line)
    if head_end < head_start:
        return entries
    head_lines = _head_line_count(root, relative)
    if head_lines is None:
        return entries
    if head_end > head_lines:
        raise ValueError(f"{path} has only {head_lines} lines; asked for {start_line}-{end_line}")
    completed = _run(
        root,
        "log",
        "--no-patch",
        f"--max-count={MAX_COMMITS}",
        f"--format=%H{_FIELD_SEP}%aI{_FIELD_SEP}%an{_FIELD_SEP}%s{_RECORD_SEP}",
        f"-L{head_start},{head_end}:{relative}",
    )
    if completed.returncode != 0:
        raise ValueError(completed.stderr.strip() or "git log failed")
    for record in completed.stdout.split(_RECORD_SEP):
        record = record.strip()
        if not record:
            continue
        sha, date, author, subject = record.split(_FIELD_SEP, 3)
        entries.append({"sha": sha, "date": date, "author": author, "subject": subject})
    return entries


def git_history_tool(repo_root: Path) -> SdkMcpTool[Any]:
    @tool(
        TOOL_NAME,
        (
            "Recent commits that touched a line range of a file in the repository "
            "under review (newest first: sha, date, author, subject). Line numbers are "
            "those of the file as it is now, the same as in the diff. Lines that are not "
            "committed yet are reported as such. Use it to tell whether a suspicious "
            "line is a fresh regression or a long-standing, deliberate choice."
        ),
        {"path": str, "start_line": int, "end_line": int},
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            commits = await asyncio.to_thread(
                git_history, repo_root, args["path"], args["start_line"], args["end_line"]
            )
        except ValueError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "is_error": True}
        return {"content": [{"type": "text", "text": json.dumps(commits, indent=1)}]}

    return handler


def build_argus_server(repo_root: Path) -> McpSdkServerConfig:
    return create_sdk_mcp_server(
        name=SERVER_NAME, version=__version__, tools=[git_history_tool(repo_root)]
    )


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _uncommitted_hunks(root: Path, relative: str) -> list[Hunk]:
    """Hunks between HEAD and the working tree for one file, in file order."""
    completed = _run(root, "diff", "-U0", "--no-color", "--no-ext-diff", "HEAD", "--", relative)
    if completed.returncode != 0:
        return []
    hunks: list[Hunk] = []
    for line in completed.stdout.splitlines():
        match = _HUNK.match(line)
        if match:
            old_start, old_len, new_start, new_len = match.groups()
            hunks.append(
                (
                    int(old_start),
                    1 if old_len is None else int(old_len),
                    int(new_start),
                    1 if new_len is None else int(new_len),
                )
            )
    return hunks


def _touches(hunk: Hunk, start: int, end: int) -> bool:
    _, _, new_start, new_len = hunk
    return new_len > 0 and new_start <= end and new_start + new_len - 1 >= start


def _to_head_range(hunks: list[Hunk], start: int, end: int) -> tuple[int, int]:
    """Map a working-tree range to HEAD, shrinking it past any uncommitted lines."""
    return _to_head(hunks, start, at_end=False), _to_head(hunks, end, at_end=True)


def _to_head(hunks: list[Hunk], line: int, *, at_end: bool) -> int:
    delta = 0
    for old_start, old_len, new_start, new_len in hunks:
        inside = new_len > 0 and new_start <= line < new_start + new_len
        if inside:
            # Snap to the committed lines just outside the changed region.
            if at_end:
                return old_start + old_len - 1 if old_len > 0 else old_start
            return old_start if old_len > 0 else old_start + 1
        after = line > new_start if new_len == 0 else line >= new_start + new_len
        if not after:
            break
        delta += old_len - new_len
    return line + delta


def _head_line_count(root: Path, relative: str) -> int | None:
    completed = _run(root, "show", f"HEAD:{relative}")
    if completed.returncode != 0:
        return None
    text = completed.stdout
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)
