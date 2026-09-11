"""The dogfood workflow's shape, checked mechanically so a careless edit cannot widen it."""

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).parent.parent / ".github" / "workflows"


def load(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / name).read_text())
    data["on"] = data.pop(True, data.get("on"))  # PyYAML reads the bare `on:` key as a boolean
    return data


def test_review_runs_on_open_and_ready_only_never_on_every_push() -> None:
    on = load("review.yml")["on"]

    assert on["pull_request"]["types"] == ["opened", "ready_for_review"]
    assert "synchronize" not in on["pull_request"]["types"]
    assert on["workflow_dispatch"]["inputs"]["pr"]["required"] is True


def test_review_permissions_are_minimal() -> None:
    wf = load("review.yml")

    assert wf["permissions"] == {"contents": "read", "pull-requests": "write"}


def test_review_cancels_a_superseded_run_for_the_same_pr() -> None:
    concurrency = load("review.yml")["concurrency"]

    assert concurrency["cancel-in-progress"] is True
    assert "pull_request.number" in concurrency["group"]
    assert "inputs.pr" in concurrency["group"]


def test_review_skips_draft_pull_requests() -> None:
    job = load("review.yml")["jobs"]["review"]

    assert "draft == false" in job["if"]


def test_review_step_uses_only_the_subscription_token_and_posts_with_an_artifact() -> None:
    steps = load("review.yml")["jobs"]["review"]["steps"]
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@"))
    review = next(s for s in steps if s.get("name") == "Review")
    upload = next(s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact@"))

    assert checkout["with"]["fetch-depth"] == 0  # git_history needs history
    assert "head.sha" in checkout["with"]["ref"]
    assert set(review["env"]) == {"CLAUDE_CODE_OAUTH_TOKEN", "GITHUB_TOKEN", "ARGUS_LOG_FORMAT"}
    assert review["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}"
    assert "ANTHROPIC_API_KEY" not in str(review)
    assert "--post" in review["run"] and "--json argus-review.json" in review["run"]
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "argus-review.json"


def test_every_workflow_pins_setup_uv_by_commit() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        for job in load(path.name)["jobs"].values():
            for step in job["steps"]:
                uses = str(step.get("uses", ""))
                if uses.startswith("astral-sh/setup-uv@"):
                    sha = uses.split("@", 1)[1].split()[0]
                    assert len(sha) == 40, f"{path.name}: {uses}"
