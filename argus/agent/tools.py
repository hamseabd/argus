"""The one custom tool Argus gives its reviewers: read-only git history.

Reviewers and the verifier need to tell a regression from an intentional
change, and that is a question for git log. They do not get Bash, so this
in-process MCP server exposes exactly one query: which commits touched a
line range of a file. It never runs anything but git log and git ls-files,
and it refuses paths outside the repository root.
"""

import asyncio
import json
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
_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"


def git_history(repo_root: Path, path: str, start_line: int, end_line: int) -> list[dict[str, str]]:
    """Commits that touched lines start_line..end_line of path, newest first.

    Returns an empty list for a file git does not track. Raises ValueError
    for a path outside the repository or a range the file does not have.
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
    line_count = _line_count(target)
    if line_count is not None and end_line > line_count:
        raise ValueError(f"{path} has only {line_count} lines; asked for {start_line}-{end_line}")
    completed = _run(
        root,
        "log",
        "--no-patch",
        f"--max-count={MAX_COMMITS}",
        f"--format=%H{_FIELD_SEP}%aI{_FIELD_SEP}%an{_FIELD_SEP}%s{_RECORD_SEP}",
        f"-L{start_line},{end_line}:{relative}",
    )
    if completed.returncode != 0:
        raise ValueError(completed.stderr.strip() or "git log failed")
    commits = []
    for record in completed.stdout.split(_RECORD_SEP):
        record = record.strip()
        if not record:
            continue
        sha, date, author, subject = record.split(_FIELD_SEP, 3)
        commits.append({"sha": sha, "date": date, "author": author, "subject": subject})
    return commits


def git_history_tool(repo_root: Path) -> SdkMcpTool[Any]:
    @tool(
        TOOL_NAME,
        (
            "Recent commits that touched a line range of a file in the repository "
            "under review (newest first: sha, date, author, subject). Use it to tell "
            "whether a suspicious line is a fresh regression or a long-standing, "
            "deliberate choice."
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


def _line_count(target: Path) -> int | None:
    try:
        with target.open("rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return None
