"""Terminal report: Markdown a person reads at the end of a run."""

from argus.domain.models import Finding, ReviewResult, rank_findings


def render_report(result: ReviewResult) -> str:
    shown = rank_findings(result.review.findings)
    rejected = sum(f.status == "rejected" for f in result.review.findings)
    parts = ["# Argus review", result.review.summary.strip(), _counts(shown, rejected)]
    parts.extend(_finding_block(f) for f in shown)
    parts.append("---")
    parts.append(_footer(result))
    return "\n\n".join(parts) + "\n"


def _counts(shown: list[Finding], rejected: int) -> str:
    counts = [
        f"{n} {status}"
        for status in ("confirmed", "unverified")
        if (n := sum(f.status == status for f in shown))
    ]
    if rejected:
        counts.append(f"{rejected} rejected and not shown")
    if not shown:
        return "No findings." + (f" {counts[0]}." if counts else "")
    noun = "finding" if len(shown) == 1 else "findings"
    return f"{len(shown)} {noun}: {', '.join(counts)}."


def _finding_block(finding: Finding) -> str:
    lines = [
        f"## [{finding.severity.upper()}] {finding.title}",
        "",
        f"`{finding.location}` · {finding.category} · {finding.status}"
        f" · confidence {finding.confidence:.2f}",
        "",
        finding.description.strip(),
        "",
        f"**Evidence:** {finding.evidence.strip()}",
    ]
    if finding.suggested_fix:
        lines += ["", "**Suggested fix:**", "", "```", finding.suggested_fix.strip(), "```"]
    return "\n".join(lines)


def _footer(result: ReviewResult) -> str:
    metrics = result.metrics
    cached = sum(m.cache_creation_input_tokens + m.cache_read_input_tokens for m in metrics)
    total_input = sum(m.input_tokens for m in metrics) + cached
    output = sum(m.output_tokens for m in metrics)
    turns = sum(m.num_turns for m in metrics)
    seconds = result.duration_ms / 1000
    subagents = sum(m.subagents_run for m in metrics)
    return (
        f"Cost ${result.total_cost_usd:.2f} · {total_input:,} input tokens ({cached:,} cached) · "
        f"{output:,} output tokens · {turns} turns · {seconds:.1f} s · {subagents} subagents · "
        f"session {result.session_id}"
    )
