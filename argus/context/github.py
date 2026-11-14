"""GitHub REST client for pull request context and for posting the review.

Six calls against the pulls API (metadata, the unified diff, the
changed-file list, the reviews and inline comments already posted, and the
review itself), plus the repository's default branch and a file's contents
there, for the review rules. Every non-2xx response and every transport
failure becomes a GitHubError carrying the status and body.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from argus import __version__
from argus.context.diff import DIFF_SIZE_CAP, cap_diff, matches_ignore, parse_diff, split_ignored
from argus.context.git import diff_between, run_git
from argus.domain.errors import GitError, GitHubError
from argus.domain.models import ChangedFile, ChangedFileStatus, PRInfo, ReviewContext
from argus.telemetry import get_logger

API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
JSON_MEDIA_TYPE = "application/vnd.github+json"
DIFF_MEDIA_TYPE = "application/vnd.github.diff"
RAW_MEDIA_TYPE = "application/vnd.github.raw+json"
_PAGE_SIZE = 100
_DIFF_TOO_LARGE = 406
_STATUS_MAP: dict[str, ChangedFileStatus] = {
    "added": "added",
    "modified": "modified",
    "removed": "removed",
    "renamed": "renamed",
    "copied": "added",
    "changed": "modified",
    "unchanged": "modified",
}
_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { isResolved isOutdated comments(first: 1) { nodes { databaseId } } }
      }
    }
  }
}
"""
_GITHUB_REMOTE = re.compile(r"github\.com[:/](?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$")


@dataclass(frozen=True)
class PostedReview:
    id: int
    url: str


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
            base_sha=data["base"].get("sha") or "",
        )

    def pr_diff(self, owner: str, repo: str, number: int) -> str:
        return self._request("GET", _pull(owner, repo, number), accept=DIFF_MEDIA_TYPE).text

    def pr_files(self, owner: str, repo: str, number: int) -> list[ChangedFile]:
        items = self._paginate(f"{_pull(owner, repo, number)}/files")
        return [_changed_file(item) for item in items]

    def pr_reviews(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        """Every review on the pull request, oldest first, as the API returns them."""
        return self._paginate(f"{_pull(owner, repo, number)}/reviews")

    def pr_review_comments(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        """Every inline review comment on the pull request, as the API returns them."""
        return self._paginate(f"{_pull(owner, repo, number)}/comments")

    def repo_review_comments(
        self, owner: str, repo: str, since: str | None = None
    ) -> list[dict[str, Any]]:
        """Every inline review comment in the repository, oldest first, optionally since a time.

        Each carries its reactions rollup, which is what `argus feedback` reads.
        """
        params: dict[str, Any] = {"sort": "created", "direction": "asc"}
        if since:
            params["since"] = since
        return self._paginate(f"/repos/{owner}/{repo}/pulls/comments", params)

    def review_comment(self, owner: str, repo: str, comment_id: int) -> dict[str, Any]:
        """One inline review comment, with its reactions and the commit it was posted on."""
        data: dict[str, Any] = self._request(
            "GET", f"/repos/{owner}/{repo}/pulls/comments/{comment_id}"
        ).json()
        return data

    def compare(self, owner: str, repo: str, base: str, head: str) -> dict[str, Any]:
        """The comparison of two commits: their merge base and the files that changed."""
        data: dict[str, Any] = self._request(
            "GET", f"/repos/{owner}/{repo}/compare/{base}...{head}"
        ).json()
        return data

    def review_threads(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        """Each review thread as {"comment_id", "resolved", "outdated"}, keyed by its first comment.

        Resolution is only in the GraphQL API. Outdated means a later commit
        changed the lines the thread was on.
        """
        threads: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            variables = {"owner": owner, "name": repo, "number": number, "after": after}
            data = self._request(
                "POST", "/graphql", json={"query": _THREADS_QUERY, "variables": variables}
            ).json()
            if data.get("errors"):
                raise GitHubError(None, json.dumps(data["errors"]))
            page = data["data"]["repository"]["pullRequest"]["reviewThreads"]
            for node in page["nodes"]:
                first = node["comments"]["nodes"]
                if first:
                    threads.append(
                        {
                            "comment_id": int(first[0]["databaseId"]),
                            "resolved": bool(node["isResolved"]),
                            "outdated": bool(node["isOutdated"]),
                        }
                    )
            if not page["pageInfo"]["hasNextPage"]:
                return threads
            after = page["pageInfo"]["endCursor"]

    def _paginate(
        self, url: str | None, extra: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        params: dict[str, Any] | None = {"per_page": _PAGE_SIZE, **(extra or {})}
        while url:
            response = self._request("GET", url, params=params)
            items.extend(response.json())
            url = response.links.get("next", {}).get("url")
            params = None  # the next link carries its own query string
        return items

    def default_branch(self, owner: str, repo: str) -> str:
        return str(self._request("GET", f"/repos/{owner}/{repo}").json()["default_branch"])

    def file_text(self, owner: str, repo: str, path: str, ref: str) -> str:
        """A file's raw contents at a ref; a missing file is a GitHubError with status 404."""
        response = self._request(
            "GET",
            f"/repos/{owner}/{repo}/contents/{path}",
            accept=RAW_MEDIA_TYPE,
            params={"ref": ref},
        )
        return response.text

    def create_check_run(self, owner: str, repo: str, payload: dict[str, Any]) -> str:
        """Create a completed check run; needs the Checks: write permission."""
        response = self._request("POST", f"/repos/{owner}/{repo}/check-runs", json=payload)
        return str(response.json()["html_url"])

    def post_review(
        self, owner: str, repo: str, number: int, payload: dict[str, Any]
    ) -> PostedReview:
        response = self._request("POST", f"{_pull(owner, repo, number)}/reviews", json=payload)
        data = response.json()
        return PostedReview(id=int(data["id"]), url=str(data["html_url"]))

    def review_comments(
        self, owner: str, repo: str, number: int, review_id: int
    ) -> list[dict[str, Any]]:
        """The inline comments of one review, with the ids GitHub gave them."""
        return self._paginate(f"{_pull(owner, repo, number)}/reviews/{review_id}/comments")

    def update_review_body(
        self, owner: str, repo: str, number: int, review_id: int, body: str
    ) -> None:
        self._request(
            "PUT", f"{_pull(owner, repo, number)}/reviews/{review_id}", json={"body": body}
        )

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
    ignore: Sequence[str] = (),
) -> ReviewContext:
    """Everything the review stage needs for a pull request, with the size cap applied.

    Ignored files leave both the diff and the changed-file list before the cap.
    """
    info = client.pr_info(owner, repo, number)
    files, ignored = split_ignored(parse_diff(_pr_diff(client, info, repo_root)), ignore)
    diff_text, truncated = cap_diff(files, max_bytes)
    changed = [
        f for f in client.pr_files(owner, repo, number) if not matches_ignore(f.path, ignore)
    ]
    return ReviewContext(
        source="pr",
        repo_root=repo_root,
        diff_text=diff_text,
        files=changed,
        truncated_files=truncated,
        ignored_files=[f.path for f in ignored],
        pr=info,
    )


def _pr_diff(client: GitHubClient, info: PRInfo, repo_root: Path) -> str:
    """The pull request's diff from GitHub, or from the checkout when GitHub will not serve it.

    The API refuses a diff past its size limits (406). The review workflow
    checks out the pull request with full history, so the same diff, from
    the merge base of base and head to head, is computed locally instead.
    """
    try:
        return client.pr_diff(info.owner, info.repo, info.number)
    except GitHubError as exc:
        if exc.status != _DIFF_TOO_LARGE or not info.base_sha:
            raise
        try:
            text = diff_between(repo_root, info.base_sha, info.head_sha)
        except GitError as git_exc:
            raise GitHubError(
                exc.status, f"{exc.body}; the local checkout could not diff it either: {git_exc}"
            ) from git_exc
        get_logger().warning("pr_diff_from_checkout", status=exc.status, pr=info.number)
        return text


def parse_repo(value: str) -> tuple[str, str]:
    """Split "owner/name" into its parts."""
    owner, sep, repo = value.partition("/")
    if not sep or not owner or not repo or "/" in repo:
        raise ValueError(f"expected owner/name, got {value!r}")
    return owner, repo


def repo_from_remote(repo_root: Path) -> str | None:
    """The "owner/name" of the origin remote when it points at GitHub, else None."""
    try:
        url = run_git(repo_root, "remote", "get-url", "origin")
    except GitError:
        return None
    match = _GITHUB_REMOTE.search(url)
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
