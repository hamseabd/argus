"""The run summary for a CI job page, such as GitHub's $GITHUB_STEP_SUMMARY.

The terminal report is for reading a review; this is for reading a run: what
was reviewed, what was found, what each stage and each agent cost, and what
configuration produced it, in tables a job page renders. It is appended to the
file the CLI is given, so a step that already wrote there keeps its text.
"""

from argus.domain.models import AgentMetrics, Finding, ReviewContext, ReviewResult, rank_findings
from argus.report.markdown import render_counts

REVIEW_STAGE = "review"


def render_summary(result: ReviewResult, context: ReviewContext) -> str:
    parts = ["## Argus review", _subject(context), render_counts(result.review.findings)]
    shown = rank_findings(result.review.findings)
    if shown:
        parts.append(_findings_table(shown))
    parts.append(_stage_table(result))
    review = next((m for m in result.metrics if m.stage == REVIEW_STAGE), None)
    if review is not None and review.agents:
        parts.append(_agent_table(review.agents))
    parts.append(_provenance(result))
    return "\n\n".join(parts) + "\n"


def _subject(context: ReviewContext) -> str:
    if context.pr is None:
        return f"Local diff, {len(context.files)} changed {_plural(len(context.files), 'file')}."
    pr = context.pr
    return f"[#{pr.number}: {_cell(pr.title)}]({pr.html_url}) at `{pr.head_sha[:7]}`"


def _findings_table(findings: list[Finding]) -> str:
    rows = [
        f"| {f.severity.upper()} | {_cell(f.title)} | `{f.location}` | {f.status} | {f.category} |"
        for f in findings
    ]
    return _table(["Severity", "Finding", "Location", "Status", "Category"], rows)


def _stage_table(result: ReviewResult) -> str:
    rows = []
    review = [m for m in result.metrics if m.stage == REVIEW_STAGE]
    verifies = [m for m in result.metrics if m.stage != REVIEW_STAGE]
    for m in review:
        rows.append(
            f"| {m.stage} | {m.model} | ${m.cost_usd:.2f} | {m.num_turns} "
            f"| {_seconds(m.duration_ms)} |"
        )
    if verifies:
        models = sorted({m.model for m in verifies})
        rows.append(
            f"| verify ({len(verifies)}) | {', '.join(models)} "
            f"| ${sum(m.cost_usd for m in verifies):.2f} "
            f"| {sum(m.num_turns for m in verifies)} "
            f"| {_seconds(sum(m.duration_ms for m in verifies))} |"
        )
    turns = sum(m.num_turns for m in result.metrics)
    rows.append(
        f"| **total** | | **${result.total_cost_usd:.2f}** | {turns} "
        f"| {_seconds(result.duration_ms)} |"
    )
    return _table(["Stage", "Model", "Cost", "Turns", "Time"], rows)


def _agent_table(agents: list[AgentMetrics]) -> str:
    rows = [
        f"| {a.agent} | {a.turns} | {a.tool_calls} | {a.output_tokens:,} "
        f"| {_seconds(a.duration_ms) if a.duration_ms else ''} |"
        for a in agents
    ]
    return _table(["Agent", "Turns", "Tool calls", "Output tokens", "Time"], rows)


def _provenance(result: ReviewResult) -> str:
    line = f"Session `{result.session_id}`."
    config = result.config
    if config is None:
        return line
    return line + (
        f"\n\nConfig: lead {config.lead_model} ({config.lead_effort}) · "
        f"specialists {config.specialist_model} ({config.specialist_effort}) · "
        f"verifier {config.verifier_model} ({config.verifier_effort}) · "
        f"prompts {config.prompts_sha} · config {config.fingerprint} · "
        f"argus {config.argus_version}"
    )


def _table(headers: list[str], rows: list[str]) -> str:
    head = "| " + " | ".join(headers) + " |"
    rule = "|" + "|".join("---" for _ in headers) + "|"
    return "\n".join([head, rule, *rows])


def _cell(text: str) -> str:
    """One table cell: no pipes, no line breaks."""
    return " ".join(text.split()).replace("|", "\\|")


def _seconds(ms: int) -> str:
    return f"{ms / 1000:.1f} s"


def _plural(count: int, noun: str) -> str:
    return noun if count == 1 else noun + "s"
