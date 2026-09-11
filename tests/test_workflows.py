"""The dogfood workflow's shape, checked mechanically so a careless edit cannot widen it."""

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).parent.parent / ".github" / "workflows"


def load(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / name).read_text())
    data["on"] = data.pop(True, data.get("on"))  # PyYAML reads the bare `on:` key as a boolean
    return data


def steps(name: str, job: str) -> list[dict]:
    return load(name)["jobs"][job]["steps"]


def test_review_runs_on_open_and_ready_only_never_on_every_push() -> None:
    on = load("review.yml")["on"]

    assert on["pull_request"]["types"] == ["opened", "ready_for_review"]
    assert on["workflow_dispatch"]["inputs"]["pr"]["required"] is True


def test_the_workflow_token_is_read_only() -> None:
    assert load("review.yml")["permissions"] == {"contents": "read"}


def test_review_cancels_a_superseded_run_for_the_same_pr() -> None:
    concurrency = load("review.yml")["concurrency"]

    assert concurrency["cancel-in-progress"] is True
    assert "pull_request.number" in concurrency["group"]
    assert "inputs.pr" in concurrency["group"]


def test_review_job_has_a_timeout() -> None:
    assert 0 < load("review.yml")["jobs"]["review"]["timeout-minutes"] <= 60


def test_review_skips_drafts_and_fork_pull_requests() -> None:
    condition = load("review.yml")["jobs"]["review"]["if"]

    assert "draft == false" in condition
    assert "head.repo.full_name == github.repository" in condition


def test_review_can_be_called_from_another_repository() -> None:
    call = load("review.yml")["on"]["workflow_call"]

    required = {name for name, spec in call["secrets"].items() if spec["required"]}
    assert required == {"CLAUDE_CODE_OAUTH_TOKEN", "ARGUS_APP_ID", "ARGUS_APP_PRIVATE_KEY"}
    assert list(call["inputs"]) == ["pr"]  # only for callers not running on a pull_request event
    assert call["inputs"]["pr"]["required"] is False


def test_argus_runs_from_a_trusted_ref_and_only_reads_the_pr_head() -> None:
    checkouts = [
        s for s in steps("review.yml", "review") if "actions/checkout@" in s.get("uses", "")
    ]
    trusted, target = checkouts

    # Here: the default branch, never the pull request's code. Called from
    # another repository: this workflow's own pinned commit. The job.* fields
    # describe the called workflow file (verified against a live run).
    assert trusted["with"]["repository"] == "${{ job.workflow_repository }}"
    assert trusted["with"]["ref"] == (
        "${{ github.repository != job.workflow_repository && job.workflow_sha"
        " || github.event.repository.default_branch }}"
    )
    assert "path" not in trusted["with"]
    assert target["with"]["path"] == "target"
    assert "head.sha" in target["with"]["ref"]
    assert target["with"]["fetch-depth"] == 0  # git_history needs history

    review = next(s for s in steps("review.yml", "review") if s.get("name") == "Review")
    assert review["working-directory"] == "target"
    assert '--project "$GITHUB_WORKSPACE"' in review["run"]
    install = next(s for s in steps("review.yml", "review") if s.get("name") == "Install")
    assert "working-directory" not in install


def test_review_step_uses_only_the_subscription_token_and_posts_with_an_artifact() -> None:
    review = next(s for s in steps("review.yml", "review") if s.get("name") == "Review")
    upload = next(
        s for s in steps("review.yml", "review") if "upload-artifact@" in s.get("uses", "")
    )

    assert set(review["env"]) == {"CLAUDE_CODE_OAUTH_TOKEN", "GITHUB_TOKEN", "ARGUS_LOG_FORMAT"}
    assert review["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}"
    assert "--post" in review["run"] and "--json" in review["run"]
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "argus-review.json"


def test_the_review_is_posted_as_the_argus_app() -> None:
    all_steps = steps("review.yml", "review")
    mint = next(s for s in all_steps if "create-github-app-token@" in s.get("uses", ""))
    review = next(s for s in all_steps if s.get("name") == "Review")

    assert mint["id"] == "argus-app"
    assert mint["with"] == {
        "app-id": "${{ secrets.ARGUS_APP_ID }}",
        "private-key": "${{ secrets.ARGUS_APP_PRIVATE_KEY }}",
    }
    assert all_steps.index(mint) == all_steps.index(review) - 1  # shortest token lifetime
    assert review["env"]["GITHUB_TOKEN"] == "${{ steps.argus-app.outputs.token }}"
    assert "secrets.GITHUB_TOKEN" not in (WORKFLOWS / "review.yml").read_text()


def test_no_workflow_mentions_an_api_key() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        assert "ANTHROPIC_API_KEY" not in path.read_text(), path.name


def test_the_review_workflow_pins_every_action_by_commit() -> None:
    for step in steps("review.yml", "review"):
        uses = step.get("uses")
        if uses:
            sha = uses.split("@", 1)[1]
            assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha), uses
