"""The Markdown run summary written to a file such as $GITHUB_STEP_SUMMARY."""

from pathlib import Path

from argus.domain.models import (
    AgentMetrics,
    Finding,
    PRInfo,
    Review,
    ReviewContext,
    ReviewResult,
    RunConfig,
    StageMetrics,
)
from argus.report.summary import render_summary

PR = PRInfo(
    owner="o",
    repo="r",
    number=7,
    title="Add paging | and lookups",
    body="",
    base_ref="main",
    head_ref="feat/x",
    head_sha="a" * 40,
    html_url="https://github.com/o/r/pull/7",
)


def finding(id: str, severity: str, status: str, title: str = "t") -> Finding:
    return Finding(
        id=id,
        file="app/repo.py",
        line=12,
        end_line=14 if id == "security-1" else None,
        severity=severity,
        category=id.split("-")[0],
        title=title,
        description="d",
        evidence="e",
        confidence=0.8,
        status=status,
    )


def context(pr: PRInfo | None = PR) -> ReviewContext:
    return ReviewContext(
        source="pr" if pr else "local",
        repo_root=Path("/repo"),
        diff_text="",
        files=[],
        pr=pr,
    )


def result(findings: list[Finding], *, config: RunConfig | None = None) -> ReviewResult:
    review = StageMetrics(
        stage="review",
        model="claude-opus-5",
        cost_usd=1.25,
        input_tokens=100,
        output_tokens=2000,
        cache_read_input_tokens=200000,
        num_turns=7,
        duration_ms=61500,
        subagents_run=3,
        agents=[
            AgentMetrics(agent="lead", turns=3, tool_calls=4, output_tokens=900),
            AgentMetrics(
                agent="security", turns=9, tool_calls=12, output_tokens=1100, duration_ms=48200
            ),
        ],
    )
    verifies = [
        StageMetrics(
            stage=f"verify:{f.id}",
            model="claude-sonnet-5",
            cost_usd=0.25,
            input_tokens=10,
            output_tokens=50,
            num_turns=3,
            duration_ms=8500,
        )
        for f in findings
        if f.status != "unverified"
    ]
    return ReviewResult(
        review=Review(
            summary="Adds paging. One real problem.", files_reviewed=[], findings=findings
        ),
        verdicts=[],
        metrics=[review, *verifies],
        total_cost_usd=round(1.25 + 0.25 * len(verifies), 2),
        duration_ms=90000,
        session_id="sess-1",
        config=config,
    )


def test_summary_opens_with_the_pull_request_and_the_counts() -> None:
    text = render_summary(result([finding("security-1", "high", "confirmed")]), context())

    assert text.startswith("## Argus review\n")
    assert "[#7](https://github.com/o/r/pull/7) Add paging \\| and lookups, at `aaaaaaa`." in text
    assert "1 finding: 1 confirmed." in text


def test_a_pull_request_title_cannot_inject_markdown_into_the_summary() -> None:
    """The title is the author's text; on the job page it must read as text, not markup."""
    hostile = PR.model_copy(
        update={"title": "evil](https://attacker.example/phish)[ ![x](https://a/b.png) `code` *em*"}
    )

    text = render_summary(result([]), context(pr=hostile))

    assert "[#7](https://github.com/o/r/pull/7)" in text
    assert "](https://attacker.example/phish)" not in text
    assert (
        "evil\\]\\(https://attacker.example/phish\\)\\[ \\!\\[x\\]\\(https://a/b.png\\) "
        "\\`code\\` \\*em\\*"
    ) in text


def test_summary_for_a_local_diff_names_no_pull_request() -> None:
    text = render_summary(result([]), context(pr=None))

    assert "#7" not in text
    assert "No findings." in text


def test_summary_tabulates_shown_findings_with_a_link_free_location_and_escaped_titles() -> None:
    findings = [
        finding("security-1", "high", "confirmed", title="SQL built with | f-string"),
        finding("quality-1", "low", "unverified"),
        finding("correctness-1", "critical", "rejected"),
    ]

    text = render_summary(result(findings), context())

    assert "| Severity | Finding | Location | Status | Category |\n" in text
    assert (
        "| HIGH | SQL built with \\| f-string | `app/repo.py:12-14` | confirmed | security |\n"
    ) in text
    assert "| LOW | t | `app/repo.py:12` | unverified | quality |\n" in text
    assert "| CRITICAL |" not in text  # rejected findings are not listed


def test_summary_tabulates_cost_by_stage_with_verifications_folded_into_one_row() -> None:
    findings = [
        finding("security-1", "high", "confirmed"),
        finding("security-2", "medium", "rejected"),
    ]

    text = render_summary(result(findings), context())

    assert "| Stage | Model | Cost | Turns | Time |" in text
    assert "| review | claude-opus-5 | $1.25 | 7 | 61.5 s |" in text
    assert "| verify (2) | claude-sonnet-5 | $0.50 | 6 | 17.0 s |" in text
    assert "| **total** | | **$1.75** | 13 | 90.0 s |" in text


def test_summary_tabulates_each_agent_of_the_review_stage() -> None:
    text = render_summary(result([]), context())

    assert "| Agent | Turns | Tool calls | Output tokens | Time |" in text
    assert "| lead | 3 | 4 | 900 |  |" in text  # no lifetime: the lead is the query
    assert "| security | 9 | 12 | 1,100 | 48.2 s |" in text


def test_summary_names_the_configuration_when_the_result_carries_it() -> None:
    config = RunConfig(
        argus_version="0.1.0",
        prompts_sha="0123456789ab",
        lead_model="claude-opus-5",
        lead_effort="high",
        lead_max_turns=40,
        lead_read_budget=10,
        lead_max_budget_usd=3.0,
        lead_max_answer_holds=3,
        specialist_model="claude-sonnet-5",
        specialist_effort="medium",
        specialist_max_turns=25,
        verifier_model="claude-sonnet-5",
        verifier_effort="medium",
        verifier_max_turns=10,
        verifier_max_budget_usd=0.5,
        verify_concurrency=4,
        diff_size_cap=204800,
    )

    text = render_summary(result([], config=config), context())
    bare = render_summary(result([]), context())

    assert (
        "Config: lead claude-opus-5 (high) · specialists claude-sonnet-5 (medium) · "
        f"verifier claude-sonnet-5 (medium) · prompts 0123456789ab · config {config.fingerprint}"
        " · argus 0.1.0"
    ) in text
    assert "Config:" not in bare
    assert "Session `sess-1`." in bare
