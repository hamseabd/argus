"""The workflow and Dependabot shapes, checked mechanically so a careless edit cannot widen them."""

from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


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


def test_ci_type_checks_the_package_between_format_and_tests() -> None:
    """mypy --strict is a gate, not advice: CI runs it on every push and pull request."""
    names = [step.get("name") for step in steps("ci.yml", "lint-and-test")]
    commands = {step.get("name"): step.get("run") for step in steps("ci.yml", "lint-and-test")}

    assert names.index("Format") < names.index("Type check") < names.index("Test")
    assert commands["Type check"] == "uv run mypy"


def test_review_step_tells_argus_the_app_identity_it_posts_as() -> None:
    """Only this login's earlier reviews are trusted; it comes from the minted app, not a guess."""
    review = next(s for s in steps("review.yml", "review") if s.get("name") == "Review")

    assert review["env"]["ARGUS_REVIEWER_LOGIN"] == (
        "${{ steps.argus-app.outputs.app-slug || steps.argus-app-checks.outputs.app-slug }}[bot]"
    )


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
    assert list(call["inputs"]) == ["pr", "fail-on"]
    # pr only for callers not running on a pull_request event; fail-on only to gate merges.
    assert call["inputs"]["pr"]["required"] is False
    assert call["inputs"]["fail-on"]["required"] is False


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
        "ARGUS_REVIEWER_LOGIN",
        "LANGSMITH_API_KEY",
        "FAIL_ON",
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
    mints = [s for s in all_steps if "create-github-app-token@" in s.get("uses", "")]
    review = next(s for s in all_steps if s.get("name") == "Review")

    _, plain, checks = mints  # the first only checks Argus out
    base = {
        "app-id": "${{ secrets.ARGUS_APP_ID }}",
        "private-key": "${{ secrets.ARGUS_APP_PRIVATE_KEY }}",
        "permission-pull-requests": "write",  # a ceiling the workflow enforces, not the app
        "permission-contents": "read",
    }
    assert (plain["id"], plain["with"]) == ("argus-app", base)
    # Checks: write only for a caller that asked for the gate; an app without
    # that permission cannot mint a token that asks for it.
    assert checks["id"] == "argus-app-checks"
    assert checks["with"] == {**base, "permission-checks": "write"}
    assert plain["if"] == "inputs.fail-on == ''"
    assert checks["if"] == "inputs.fail-on != ''"
    assert all_steps.index(checks) == all_steps.index(review) - 1  # shortest token lifetime
    assert all_steps.index(plain) == all_steps.index(review) - 2
    assert review["env"]["GITHUB_TOKEN"] == (
        "${{ steps.argus-app.outputs.token || steps.argus-app-checks.outputs.token }}"
    )
    assert "secrets.GITHUB_TOKEN" not in (WORKFLOWS / "review.yml").read_text()


def test_argus_is_checked_out_with_its_own_read_only_app_token() -> None:
    """Argus's repository is private, so the caller's token cannot check it out.

    The token that can is scoped to that repository alone and may only read it;
    it never posts, and the review token never reaches Argus's repository.
    """
    all_steps = steps("review.yml", "review")
    resolve = next(s for s in all_steps if s.get("id") == "argus-repo")
    mint = next(s for s in all_steps if s.get("id") == "argus-code")
    trusted, target = (s for s in all_steps if "actions/checkout@" in s.get("uses", ""))

    assert resolve["env"] == {"ARGUS_REPOSITORY": "${{ job.workflow_repository }}"}
    assert 'echo "owner=${ARGUS_REPOSITORY%%/*}" >> "$GITHUB_OUTPUT"' in resolve["run"]
    assert 'echo "name=${ARGUS_REPOSITORY#*/}" >> "$GITHUB_OUTPUT"' in resolve["run"]
    assert mint["with"] == {
        "app-id": "${{ secrets.ARGUS_APP_ID }}",
        "private-key": "${{ secrets.ARGUS_APP_PRIVATE_KEY }}",
        "owner": "${{ steps.argus-repo.outputs.owner }}",
        "repositories": "${{ steps.argus-repo.outputs.name }}",
        "permission-contents": "read",
    }
    assert all_steps.index(resolve) < all_steps.index(mint) < all_steps.index(trusted)
    assert trusted["with"]["token"] == "${{ steps.argus-code.outputs.token }}"
    assert trusted["with"]["persist-credentials"] is False
    assert "token" not in target["with"]  # the pull request head keeps the caller's token
    assert "argus-code" not in str(next(s for s in all_steps if s.get("name") == "Review"))


def test_a_caller_can_gate_merges_on_an_argus_check_run() -> None:
    fail_on = load("review.yml")["on"]["workflow_call"]["inputs"]["fail-on"]
    review = next(s for s in steps("review.yml", "review") if s.get("name") == "Review")

    assert fail_on["type"] == "string" and fail_on["default"] == ""
    assert review["env"]["FAIL_ON"] == "${{ inputs.fail-on }}"  # never interpolated into the script
    assert '--check --fail-on "$FAIL_ON"' in review["run"]


def test_no_workflow_mentions_an_api_key() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        assert "ANTHROPIC_API_KEY" not in path.read_text(), path.name


WRITERS = {("eval.yml", "comment"): {"pull-requests": "write"}}
"""The one job that writes: it posts the eval table, holds no secret, runs no repository code."""


def test_every_workflow_job_pins_its_own_read_only_token() -> None:
    """A job without its own block inherits the workflow's, so widening the top level widens it."""
    for path in WORKFLOWS.glob("*.y*ml"):
        for name, job in load(path.name)["jobs"].items():
            expected = WRITERS.get((path.name, name), {"contents": "read"})
            assert job.get("permissions") == expected, f"{path.name}:{name}"


def test_the_repository_publishes_a_security_policy() -> None:
    assert (ROOT / "SECURITY.md").is_file()


def test_the_workflows_and_the_pipeline_need_the_owners_review() -> None:
    lines = (ROOT / ".github" / "CODEOWNERS").read_text().splitlines()
    rules = [line.split() for line in lines if line.strip() and not line.startswith("#")]
    owners = {pattern: set(names) for pattern, *names in rules}

    # The whole of .github/, so CODEOWNERS guards itself; argus/ holds the prompts too.
    for path in ("/.github/", "/argus/"):
        assert "@hamseabd" in owners[path], path


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


def test_the_eval_token_is_read_only_and_the_job_has_a_timeout() -> None:
    eval_workflow = load("eval.yml")

    assert eval_workflow["permissions"] == {"contents": "read"}
    assert 0 < eval_workflow["jobs"]["eval"]["timeout-minutes"] <= 360  # GitHub's job maximum


def test_the_eval_uses_only_the_subscription_token_and_keeps_its_record() -> None:
    all_steps = steps("eval.yml", "eval")
    run = next(s for s in all_steps if s.get("name") == "Evaluate")
    upload = next(s for s in all_steps if "upload-artifact@" in s.get("uses", ""))

    assert set(run["env"]) == {
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ARGUS_LOG_FORMAT",
        "EVAL_MODE",
        "EVAL_TRIALS",
        "EVENT",
        "LABEL",
    }
    # Inputs reach the script through the environment, never interpolated into it.
    assert run["env"]["EVAL_MODE"] == "${{ inputs.mode }}"
    assert run["env"]["EVAL_TRIALS"] == "${{ inputs.trials }}"
    assert '--trials "$EVAL_TRIALS"' in run["run"]
    assert run["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}"
    assert "python -m evals.run" in run["run"]
    assert upload["if"] == "always()"
    assert upload["with"]["path"].split() == ["evals/results/", "evals/report.md"]


def test_the_eval_workflow_pins_every_action_by_commit() -> None:
    for step in [s for job in ("eval", "gate", "comment") for s in steps("eval.yml", job)]:
        uses = step.get("uses")
        if uses:
            sha = uses.split("@", 1)[1]
            assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha), uses


def test_the_pr_head_checkout_leaves_no_token_in_its_git_config() -> None:
    # The reviewers read the target directory; a persisted credential would
    # sit in target/.git/config for a prompt injection to ask for.
    target = next(
        s
        for s in steps("review.yml", "review")
        if "actions/checkout@" in s.get("uses", "") and s.get("with", {}).get("path") == "target"
    )
    assert target["with"]["persist-credentials"] is False


def test_three_trials_of_both_modes_are_refused_before_spending_quota() -> None:
    # The job's six-hour cap cannot hold them; refuse up front, not partway through.
    guard = steps("eval.yml", "eval")[0]

    assert guard["if"] == "inputs.mode == 'both' && inputs.trials == '3'"
    assert "exit 1" in guard["run"]


def test_eval_runs_on_pull_requests_that_touch_argus_or_the_evals_and_on_demand() -> None:
    on = load("eval.yml")["on"]

    assert set(on["pull_request"]["paths"]) == {"argus/**", "evals/**"}
    assert on["pull_request"]["types"] == ["opened", "synchronize", "reopened", "labeled"]
    assert "workflow_dispatch" in on
    assert "pull_request_target" not in on


def test_eval_never_runs_a_fork_pull_request_and_only_the_full_label_starts_a_labeled_run() -> None:
    condition = load("eval.yml")["jobs"]["eval"]["if"]

    assert "github.event.pull_request.head.repo.full_name == github.repository" in condition
    assert "github.event.label.name == 'eval-full'" in condition


def test_eval_tokens_are_least_privilege_and_only_the_comment_job_can_write() -> None:
    workflow = load("eval.yml")

    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["eval"]["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["comment"]["permissions"] == {"pull-requests": "write"}
    comment_env = [s.get("env", {}) for s in steps("eval.yml", "comment")]
    assert not any("CLAUDE_CODE_OAUTH_TOKEN" in env for env in comment_env)


def test_a_push_cancels_only_its_own_pull_requests_smoke_run() -> None:
    """Never another pull request's run, and never an eval-full run."""
    concurrency = load("eval.yml")["jobs"]["eval"]["concurrency"]

    assert "github.event.pull_request.number" in concurrency["group"]
    assert "'full' || 'smoke'" in concurrency["group"]
    assert "github.run_id" in concurrency["group"]
    assert concurrency["cancel-in-progress"] == (
        "${{ github.event_name == 'pull_request' && github.event.label.name != 'eval-full' }}"
    )


def test_a_pull_request_reviews_the_default_branchs_smoke_cases() -> None:
    evaluate = next(s for s in steps("eval.yml", "eval") if s.get("name") == "Evaluate")

    assert '--thresholds "$SMOKE_FROM/evals/thresholds.yaml"' in evaluate["run"]
    assert '--cases "$SMOKE_FROM/evals/cases"' in evaluate["run"]
    assert all("evals.gate" not in str(s.get("run", "")) for s in steps("eval.yml", "eval"))


def test_the_gate_runs_in_its_own_job_from_fresh_checkouts_and_only_the_record() -> None:
    job = load("eval.yml")["jobs"]["gate"]
    checkouts = [s for s in job["steps"] if "checkout@" in s.get("uses", "")]
    gate = next(s for s in job["steps"] if s.get("name") == "Compare with the baseline")
    download = next(s for s in job["steps"] if "download-artifact@" in s.get("uses", ""))

    assert job["needs"] == "eval"
    assert job["permissions"] == {"contents": "read"}
    assert [c["with"]["ref"] for c in checkouts] == [
        "${{ github.event.repository.default_branch }}",
        "${{ github.event.pull_request.head.sha }}",
    ]
    assert all(c["with"]["persist-credentials"] is False for c in checkouts)
    assert download["with"]["name"] == "argus-eval"
    assert 'cd "$GATE"' in gate["run"]
    assert '--corpus "$GITHUB_WORKSPACE/change/evals/cases"' in gate["run"]
    assert "no record" in gate["run"]


def test_the_eval_table_is_one_comment_updated_in_place_and_a_missing_table_is_said() -> None:
    (post,) = [s for s in steps("eval.yml", "comment") if "run" in s]
    download = next(
        s for s in steps("eval.yml", "comment") if "download-artifact@" in s.get("uses", "")
    )

    assert "<!-- argus:eval -->" in post["run"]
    assert "-X PATCH" in post["run"]
    assert "gate stopped before writing one" in post["run"]
    assert download["continue-on-error"] is True
