from pathlib import Path

from argus.agent.prompts import PROMPT_NAMES, lead_user_prompt, load_prompt, verifier_user_prompt
from argus.domain.models import ChangedFile, Finding, PRInfo, ReviewContext

FIXTURES = Path(__file__).parent / "fixtures" / "diffs"


def context(pr: PRInfo | None = None, truncated: list[str] | None = None) -> ReviewContext:
    return ReviewContext(
        source="pr" if pr else "local",
        repo_root=Path("/repo"),
        diff_text=(FIXTURES / "mixed.diff").read_text(),
        files=[
            ChangedFile(path="pkg/module.py", status="modified"),
            ChangedFile(path="pkg/new_name.py", status="renamed", previous_path="pkg/old_name.py"),
        ],
        truncated_files=truncated or [],
        pr=pr,
    )


def finding() -> Finding:
    return Finding(
        id="security-1",
        file="pkg/module.py",
        line=3,
        end_line=4,
        severity="high",
        category="security",
        title="Injection",
        description="Untrusted input reaches a query.",
        evidence="line 3 concatenates.",
        suggested_fix="Use parameters.",
        confidence=0.8,
    )


def test_every_prompt_file_exists_and_is_not_empty() -> None:
    assert set(PROMPT_NAMES) == {"lead", "correctness", "security", "quality", "verifier"}
    for name in PROMPT_NAMES:
        assert len(load_prompt(name)) > 200


def test_lead_prompt_makes_delegation_mandatory_and_names_the_output() -> None:
    text = load_prompt("lead")

    for specialist in ("correctness", "security", "quality"):
        assert specialist in text
    assert "all three" in text
    assert "git_history" in text


def test_specialist_prompts_share_the_finding_contract() -> None:
    for name in ("correctness", "security", "quality"):
        text = load_prompt(name)
        assert "JSON array" in text
        for field in ("file", "line", "severity", "category", "title", "evidence", "confidence"):
            assert f'"{field}"' in text
        assert f'"category": "{name}"' in text


def test_verifier_prompt_tries_to_refute() -> None:
    text = load_prompt("verifier")

    assert "refute" in text
    assert "confirmed" in text
    assert "rejected" in text


def test_lead_user_prompt_lists_files_diff_and_truncation() -> None:
    text = lead_user_prompt(context(truncated=["big/generated.json"]))

    assert "pkg/module.py (modified)" in text
    assert "pkg/new_name.py (renamed from pkg/old_name.py)" in text
    assert "big/generated.json" in text
    assert "diff --git a/pkg/module.py" in text
    assert "```diff" in text
    assert "Pull request" not in text


def test_lead_user_prompt_includes_pr_title_and_body() -> None:
    pr = PRInfo(
        owner="o",
        repo="r",
        number=7,
        title="Add caching",
        body="Caches lookups.\n",
        base_ref="main",
        head_ref="feat/cache",
        head_sha="a" * 40,
        html_url="https://x",
    )
    text = lead_user_prompt(context(pr=pr))

    assert "Add caching" in text
    assert "Caches lookups." in text
    assert "generated.json" not in text
    assert "omitted" not in text.lower()


def test_verifier_user_prompt_carries_the_finding_and_its_hunk() -> None:
    text = verifier_user_prompt(finding(), "diff --git a/pkg/module.py b/pkg/module.py\n+x\n")

    for expected in (
        "pkg/module.py",
        "3-4",
        "high",
        "security",
        "Injection",
        "Untrusted input",
        "line 3 concatenates",
        "Use parameters",
        "```diff",
    ):
        assert expected in text
