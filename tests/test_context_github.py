import subprocess
from pathlib import Path

import httpx
import pytest
import respx

from argus.context.github import GitHubClient, parse_repo, pr_context, repo_from_remote
from argus.domain.errors import GitHubError
from argus.domain.models import ChangedFile

API = "https://api.github.com"
PR_JSON = {
    "number": 7,
    "title": "Add caching",
    "body": "Caches lookups.",
    "html_url": "https://github.com/o/r/pull/7",
    "base": {"ref": "main"},
    "head": {"ref": "feat/cache", "sha": "a" * 40},
}
DIFF = (
    "diff --git a/app/cache.py b/app/cache.py\n--- a/app/cache.py\n+++ b/app/cache.py\n"
    "@@ -1,2 +1,3 @@\n a\n+b\n c\n"
)


def client() -> GitHubClient:
    return GitHubClient(token="t0k")


@respx.mock
def test_pr_info_maps_the_payload_and_sends_the_token() -> None:
    route = respx.get(f"{API}/repos/o/r/pulls/7").mock(
        return_value=httpx.Response(200, json=PR_JSON)
    )

    info = client().pr_info("o", "r", 7)

    assert info.owner == "o" and info.repo == "r" and info.number == 7
    assert info.title == "Add caching"
    assert info.body == "Caches lookups."
    assert info.base_ref == "main" and info.head_ref == "feat/cache"
    assert info.head_sha == "a" * 40
    assert info.html_url == "https://github.com/o/r/pull/7"
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer t0k"
    assert sent.headers["Accept"] == "application/vnd.github+json"
    assert sent.headers["X-GitHub-Api-Version"]


@respx.mock
def test_pr_info_with_a_null_body_is_an_empty_string() -> None:
    respx.get(f"{API}/repos/o/r/pulls/7").mock(
        return_value=httpx.Response(200, json={**PR_JSON, "body": None})
    )

    assert client().pr_info("o", "r", 7).body == ""


@respx.mock
def test_pr_diff_asks_for_the_diff_media_type() -> None:
    route = respx.get(f"{API}/repos/o/r/pulls/7").mock(return_value=httpx.Response(200, text=DIFF))

    assert client().pr_diff("o", "r", 7) == DIFF
    assert route.calls.last.request.headers["Accept"] == "application/vnd.github.diff"


@respx.mock
def test_pr_files_paginate_and_map_statuses() -> None:
    page1 = [
        {"filename": "a.py", "status": "added"},
        {"filename": "b.py", "status": "modified"},
        {"filename": "c.py", "status": "removed"},
        {"filename": "d2.py", "status": "renamed", "previous_filename": "d.py"},
    ]
    page2 = [{"filename": "e.py", "status": "copied", "previous_filename": "e0.py"}]
    route = respx.get(f"{API}/repos/o/r/pulls/7/files")
    route.side_effect = [
        httpx.Response(
            200, json=page1, headers={"Link": f'<{API}/repos/o/r/pulls/7/files?page=2>; rel="next"'}
        ),
        httpx.Response(200, json=page2),
    ]

    files = client().pr_files("o", "r", 7)

    assert files == [
        ChangedFile(path="a.py", status="added"),
        ChangedFile(path="b.py", status="modified"),
        ChangedFile(path="c.py", status="removed"),
        ChangedFile(path="d2.py", status="renamed", previous_path="d.py"),
        ChangedFile(path="e.py", status="added", previous_path="e0.py"),
    ]
    assert route.call_count == 2
    assert route.calls[0].request.url.params["per_page"] == "100"


@respx.mock
def test_post_review_sends_the_payload_and_returns_the_url() -> None:
    route = respx.post(f"{API}/repos/o/r/pulls/7/reviews").mock(
        return_value=httpx.Response(
            200, json={"html_url": "https://github.com/o/r/pull/7#pullrequestreview-1"}
        )
    )
    payload = {"commit_id": "a" * 40, "event": "COMMENT", "body": "b", "comments": []}

    url = client().post_review("o", "r", 7, payload)

    assert url == "https://github.com/o/r/pull/7#pullrequestreview-1"
    assert route.calls.last.request.read() == httpx.Request("POST", "x", json=payload).read()


@respx.mock
def test_non_2xx_is_a_github_error_with_status_and_body() -> None:
    respx.get(f"{API}/repos/o/r/pulls/7").mock(
        return_value=httpx.Response(422, json={"message": "Unprocessable Entity"})
    )

    with pytest.raises(GitHubError) as info:
        client().pr_info("o", "r", 7)

    assert info.value.status == 422
    assert "Unprocessable" in info.value.body


@respx.mock
def test_transport_errors_are_github_errors_too() -> None:
    respx.get(f"{API}/repos/o/r/pulls/7").mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(GitHubError, match="boom"):
        client().pr_info("o", "r", 7)


@respx.mock
def test_pr_context_assembles_diff_files_metadata_and_the_cap(tmp_path: Path) -> None:
    respx.get(f"{API}/repos/o/r/pulls/7", headers={"Accept": "application/vnd.github.diff"}).mock(
        return_value=httpx.Response(200, text=DIFF)
    )
    respx.get(f"{API}/repos/o/r/pulls/7").mock(return_value=httpx.Response(200, json=PR_JSON))
    respx.get(f"{API}/repos/o/r/pulls/7/files").mock(
        return_value=httpx.Response(200, json=[{"filename": "app/cache.py", "status": "modified"}])
    )

    ctx = pr_context(client(), "o", "r", 7, tmp_path)
    capped = pr_context(client(), "o", "r", 7, tmp_path, max_bytes=10)

    assert ctx.source == "pr"
    assert ctx.repo_root == tmp_path
    assert ctx.diff_text == DIFF
    assert ctx.files == [ChangedFile(path="app/cache.py", status="modified")]
    assert ctx.pr is not None and ctx.pr.head_sha == "a" * 40
    assert ctx.truncated_files == []
    assert capped.diff_text == ""
    assert capped.truncated_files == ["app/cache.py"]


def test_parse_repo_accepts_owner_slash_name_only() -> None:
    assert parse_repo("hamseabd/argus") == ("hamseabd", "argus")
    for bad in ("argus", "a/b/c", "", "/argus", "hamseabd/"):
        with pytest.raises(ValueError):
            parse_repo(bad)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/hamseabd/argus.git",
        "https://github.com/hamseabd/argus",
        "git@github.com:hamseabd/argus.git",
        "ssh://git@github.com/hamseabd/argus.git",
    ],
)
def test_repo_from_remote_reads_the_origin_url(tmp_path: Path, url: str) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "remote", "add", "origin", url], cwd=tmp_path, check=True)

    assert repo_from_remote(tmp_path) == "hamseabd/argus"


def test_repo_from_remote_is_none_without_a_github_origin(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert repo_from_remote(tmp_path) is None

    subprocess.run(
        ["git", "remote", "add", "origin", "https://gitlab.com/x/y.git"], cwd=tmp_path, check=True
    )
    assert repo_from_remote(tmp_path) is None
