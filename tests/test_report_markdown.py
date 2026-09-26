from argus.domain.models import AgentMetrics, Finding, Review, ReviewResult, StageMetrics, Verdict
from argus.report.markdown import render_report


def result(findings: list[Finding], verdicts: list[Verdict] | None = None) -> ReviewResult:
    return ReviewResult(
        review=Review(
            summary="Adds paging and a lookup. One real problem.",
            files_reviewed=["a.py", "b.py"],
            findings=findings,
        ),
        verdicts=verdicts or [],
        metrics=[
            StageMetrics(
                stage="review",
                model="claude-opus-5",
                cost_usd=1.25,
                input_tokens=1000,
                output_tokens=200,
                cache_creation_input_tokens=20000,
                cache_read_input_tokens=5000,
                num_turns=7,
                duration_ms=61500,
                subagents_run=3,
            ),
            StageMetrics(
                stage="verify:security-1",
                model="claude-sonnet-5",
                cost_usd=0.25,
                input_tokens=100,
                output_tokens=50,
                num_turns=3,
                duration_ms=8500,
            ),
        ],
        total_cost_usd=1.5,
        duration_ms=70000,
        session_id="sess-1",
    )


def test_footer_lists_each_agent_when_the_metrics_carry_them() -> None:
    plain = result([])
    agents = [
        AgentMetrics(agent="lead", turns=4, tool_calls=1),
        AgentMetrics(agent="security", turns=9, tool_calls=12, duration_ms=48200),
        AgentMetrics(agent="quality", turns=15, tool_calls=21, duration_ms=61000),
    ]
    with_agents = plain.model_copy(
        update={"metrics": [plain.metrics[0].model_copy(update={"agents": agents})]}
    )

    assert "Agents:" not in render_report(plain)
    assert (
        "\n\nAgents: lead 4 turns, 1 tool call · security 9 turns, 12 tool calls, 48.2 s · "
        "quality 15 turns, 21 tool calls, 61.0 s"
    ) in render_report(with_agents)  # its own paragraph, so no renderer folds it into the footer


def test_footer_mentions_schema_rejections_only_when_there_were_any() -> None:
    clean = result([])
    noisy = clean.model_copy(
        update={"metrics": [clean.metrics[0].model_copy(update={"output_rejections": 3})]}
    )

    assert "schema rejections" not in render_report(clean)
    assert "· 3 schema rejections ·" in render_report(noisy)


def test_footer_mentions_held_and_recovered_answers_only_when_there_were_any() -> None:
    clean = result([])
    held = clean.model_copy(
        update={
            "metrics": [
                clean.metrics[0].model_copy(
                    update={"answers_held": 2, "structured_output_recovered": True}
                )
            ]
        }
    )

    assert "held" not in render_report(clean)
    assert "recovered" not in render_report(clean)
    assert "· 2 answers held · answer recovered ·" in render_report(held)


def finding(**overrides) -> Finding:
    base = dict(
        id="security-1",
        file="a.py",
        line=8,
        severity="critical",
        category="security",
        title="SQL built from user input",
        description="The name is interpolated into the query.",
        evidence="`f\"... WHERE name = '{name}'\"` on line 8.",
        suggested_fix="Use a parameter:\nconn.execute(q, (name,))",
        confidence=0.95,
        status="confirmed",
    )
    return Finding(**{**base, **overrides})


EXPECTED = """\
# Argus review

Adds paging and a lookup. One real problem.

2 findings: 1 confirmed, 1 unverified, 1 rejected and not shown.

## [CRITICAL] SQL built from user input

`a.py:8` · security · confirmed · confidence 0.95

The name is interpolated into the query.

**Evidence:** `f"... WHERE name = '{name}'"` on line 8.

**Suggested fix:**

```
Use a parameter:
conn.execute(q, (name,))
```

## [LOW] Unused helper

`b.py:3-4` · quality · unverified · confidence 0.40

Dead code.

**Evidence:** Nothing calls it.

---

Cost $1.50 · 26,100 input tokens (25,000 cached) · 250 output tokens · 10 turns · 70.0 s \
· 3 subagents · session sess-1
"""


def test_report_renders_ranked_findings_and_the_footer() -> None:
    findings = [
        finding(
            id="quality-1",
            file="b.py",
            line=3,
            end_line=4,
            severity="low",
            category="quality",
            title="Unused helper",
            description="Dead code.",
            evidence="Nothing calls it.",
            suggested_fix=None,
            confidence=0.4,
            status="unverified",
        ),
        finding(
            id="correctness-1",
            severity="high",
            category="correctness",
            title="Rejected one",
            status="rejected",
        ),
        finding(),
    ]

    assert render_report(result(findings)) == EXPECTED


def test_report_with_no_findings_says_so() -> None:
    text = render_report(result([]))

    assert "No findings." in text
    assert "Cost $1.50" in text
    assert "## " not in text


def test_report_with_only_rejected_findings_says_so() -> None:
    text = render_report(result([finding(status="rejected")]))

    assert "No findings. 1 rejected and not shown." in text


def test_report_counts_do_not_mention_absent_categories() -> None:
    text = render_report(result([finding()]))

    assert "1 finding: 1 confirmed." in text
    assert "rejected" not in text
