"""GitHub REST client for pull request context and for posting the review.

Four calls, all against the pulls API: metadata, the unified diff, the
changed-file list, and the review itself. Every non-2xx response and every
transport failure becomes a GitHubError carrying the status and body.
"""

import re
import subprocess
from pathlib import Path
from typing import Any

import httpx

from argus import __version__
from argus.context.diff import DIFF_SIZE_CAP, cap_diff, parse_diff
from argus.domain.errors import GitHubError
from argus.domain.models import ChangedFile, ChangedFileStatus, PRInfo, ReviewContext

API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
JSON_MEDIA_TYPE = "application/vnd.github+json"
DIFF_MEDIA_TYPE = "application/vnd.github.diff"
_PAGE_SIZE = 100
_STATUS_MAP: dict[str, ChangedFileStatus] = {
    "added": "added",
    "modified": "modified",
    "removed": "removed",
    "renamed": "renamed",
    "copied": "added",
    "changed": "modified",
    "unchanged": "modified",
}
_GITHUB_REMOTE = re.compile(r"github\.com[:/](?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$")


class GitHubClient:
    def __init__(
        self, token: str, base_url: str = API_URL, client: httpx.Client | None = None
    ) -> None:
        self._client = client or httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": f"argus/{__version__}",
            },
            timeout=30.0,
        )

    def pr_info(self, owner: str, repo: str, number: int) -> PRInfo:
        data = self._request("GET", _pull(owner, repo, number)).json()
        return PRInfo(
            owner=owner,
            repo=repo,
            number=number,
            title=data.get("title") or "",
            body=data.get("body") or "",
            base_ref=data["base"]["ref"],
            head_ref=data["head"]["ref"],
            head_sha=data["head"]["sha"],
            html_url=data["html_url"],
        )

    def pr_diff(self, owner: str, repo: str, number: int) -> str:
        return self._request("GET", _pull(owner, repo, number), accept=DIFF_MEDIA_TYPE).text

    def pr_files(self, owner: str, repo: str, number: int) -> list[ChangedFile]:
        files: list[ChangedFile] = []
        url: str | None = f"{_pull(owner, repo, number)}/files"
        params: dict[str, Any] | None = {"per_page": _PAGE_SIZE}
        while url:
            response = self._request("GET", url, params=params)
            files.extend(_changed_file(item) for item in response.json())
            url = response.links.get("next", {}).get("url")
            params = None  # the next link carries its own query string
        return files

    def post_review(self, owner: str, repo: str, number: int, payload: dict[str, Any]) -> str:
        response = self._request("POST", f"{_pull(owner, repo, number)}/reviews", json=payload)
        return response.json()["html_url"]

    def _request(
        self, method: str, url: str, *, accept: str = JSON_MEDIA_TYPE, **kwargs: Any
    ) -> httpx.Response:
        try:
            response = self._client.request(method, url, headers={"Accept": accept}, **kwargs)
        except httpx.HTTPError as exc:
            raise GitHubError(None, f"{type(exc).__name__}: {exc}") from exc
        if not response.is_success:
            raise GitHubError(response.status_code, response.text)
        return response


def pr_context(
    client: GitHubClient,
    owner: str,
    repo: str,
    number: int,
    repo_root: Path,
    max_bytes: int = DIFF_SIZE_CAP,
) -> ReviewContext:
    """Everything the review stage needs for a pull request, with the size cap applied."""
    info = client.pr_info(owner, repo, number)
    diff_text, truncated = cap_diff(parse_diff(client.pr_diff(owner, repo, number)), max_bytes)
    return ReviewContext(
        source="pr",
        repo_root=repo_root,
        diff_text=diff_text,
        files=client.pr_files(owner, repo, number),
        truncated_files=truncated,
        pr=info,
    )


def parse_repo(value: str) -> tuple[str, str]:
    """Split "owner/name" into its parts."""
    owner, sep, repo = value.partition("/")
    if not sep or not owner or not repo or "/" in repo:
        raise ValueError(f"expected owner/name, got {value!r}")
    return owner, repo


def repo_from_remote(repo_root: Path) -> str | None:
    """The "owner/name" of the origin remote when it points at GitHub, else None."""
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        return None
    match = _GITHUB_REMOTE.search(completed.stdout.strip())
    if match is None:
        return None
    return f"{match['owner']}/{match['repo']}"


def _pull(owner: str, repo: str, number: int) -> str:
    return f"/repos/{owner}/{repo}/pulls/{number}"


def _changed_file(item: dict[str, Any]) -> ChangedFile:
    return ChangedFile(
        path=item["filename"],
        status=_STATUS_MAP.get(item.get("status", ""), "modified"),
        previous_path=item.get("previous_filename"),
    )
