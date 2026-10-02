"""The workflow and Dependabot shapes, checked mechanically so a careless edit cannot widen them."""

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


def test_review_skips_pull_requests_opened_by_dependabot() -> None:
    """Dependabot's pull requests get no secrets, so the token mint fails, not the review.

    Skipping them keeps a dependency bump from showing a red check that says
    nothing about the change. A maintainer can still review one on demand.

    The clause has to sit inside the pull_request group: joined at the top
    level with `||` it would let every draft and every fork through, which is
    why this asserts where the clause is and not just that it is there.
    """
    condition = load("review.yml")["jobs"]["review"]["if"]
    group = condition[condition.index("(") + 1 : condition.rindex(")")]

    assert "pull_request.user.login != 'dependabot[bot]'" in group
    assert "||" not in group  # every clause in the group is an AND
    assert "draft == false" in group
    assert "head.repo.full_name == github.repository" in group


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

    assert set(review["env"]) == {
        "CLAUDE_CODE_OAUTH_TOKEN",
        "GITHUB_TOKEN",
        "ARGUS_LOG_FORMAT",
        "LANGSMITH_API_KEY",
    }
    assert review["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}"
    assert "--post" in review["run"] and "--json" in review["run"]
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "argus-review.json"


def test_tracing_is_optional_for_callers_and_off_without_the_key() -> None:
    call = load("review.yml")["on"]["workflow_call"]
    review = next(s for s in steps("review.yml", "review") if s.get("name") == "Review")

    assert call["secrets"]["LANGSMITH_API_KEY"]["required"] is False
    assert review["env"]["LANGSMITH_API_KEY"] == "${{ secrets.LANGSMITH_API_KEY }}"
    guard = 'if [ -n "$LANGSMITH_API_KEY" ]; then'
    run = review["run"]
    assert guard in run
    before, _, after = run.partition(guard)
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in before
    inside, _, _ = after.partition("fi\n")
    assert "export OTEL_EXPORTER_OTLP_ENDPOINT=https://api.smith.langchain.com/otel" in inside
    assert 'export OTEL_EXPORTER_OTLP_HEADERS="x-api-key=$LANGSMITH_API_KEY' in inside


def test_the_review_is_posted_as_the_argus_app() -> None:
    all_steps = steps("review.yml", "review")
    mint = next(s for s in all_steps if "create-github-app-token@" in s.get("uses", ""))
    review = next(s for s in all_steps if s.get("name") == "Review")

    assert mint["id"] == "argus-app"
    assert mint["with"] == {
        "app-id": "${{ secrets.ARGUS_APP_ID }}",
        "private-key": "${{ secrets.ARGUS_APP_PRIVATE_KEY }}",
        "permission-pull-requests": "write",  # a ceiling the workflow enforces, not the app
        "permission-contents": "read",
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


def dependabot() -> dict:
    return yaml.safe_load((WORKFLOWS.parent / "dependabot.yml").read_text())


def test_dependabot_uses_the_current_config_schema() -> None:
    assert dependabot()["version"] == 2


def test_dependabot_watches_the_pinned_actions_and_the_python_dependencies() -> None:
    ecosystems = {update["package-ecosystem"] for update in dependabot()["updates"]}

    assert ecosystems == {"github-actions", "uv"}


def test_every_dependabot_update_checks_the_repository_root_weekly() -> None:
    for update in dependabot()["updates"]:
        assert update["directory"] == "/", update["package-ecosystem"]
        assert update["schedule"]["interval"] == "weekly", update["package-ecosystem"]


def test_dependabot_batches_minor_and_patch_into_one_pull_request_per_ecosystem() -> None:
    for update in dependabot()["updates"]:
        (group,) = update["groups"].values()  # one group, so one PR, majors still separate

        assert group["patterns"] == ["*"], update["package-ecosystem"]
        assert set(group["update-types"]) == {"minor", "patch"}, update["package-ecosystem"]
