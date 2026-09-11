"""Markdown rendering shared by the terminal report and the GitHub review."""

from argus.domain.models import Finding, ReviewResult, rank_findings


def render_report(result: ReviewResult) -> str:
    """The terminal report: summary, ranked findings, metrics footer."""
    parts = ["# Argus review", result.review.summary.strip(), render_counts(result.review.findings)]
    parts.extend(render_finding(f) for f in rank_findings(result.review.findings))
    parts.append("---")
    parts.append(render_footer(result))
    return "\n\n".join(parts) + "\n"


def render_counts(findings: list[Finding]) -> str:
    """One line: how many findings are shown, by status, and how many were rejected."""
    shown = rank_findings(findings)
    rejected = sum(f.status == "rejected" for f in findings)
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


def render_finding(finding: Finding, heading: str = "##") -> str:
    """A finding as a titled section with its location on the line below."""
    lines = [
        f"{heading} [{finding.severity.upper()}] {finding.title}",
        "",
        f"`{finding.location}` · {_meta(finding)}",
        "",
        *_details(finding),
    ]
    return "\n".join(lines)


def finding_body(finding: Finding) -> str:
    """A finding as an inline comment body; the location is the comment's anchor."""
    return "\n".join(
        [
            f"**[{finding.severity.upper()}] {finding.title}** · {_meta(finding)}",
            "",
            *_details(finding),
        ]
    )


def render_footer(result: ReviewResult) -> str:
    metrics = result.metrics
    cached = sum(m.cache_creation_input_tokens + m.cache_read_input_tokens for m in metrics)
    total_input = sum(m.input_tokens for m in metrics) + cached
    output = sum(m.output_tokens for m in metrics)
    turns = sum(m.num_turns for m in metrics)
    seconds = result.duration_ms / 1000
    subagents = sum(m.subagents_run for m in metrics)
    rejections = sum(m.output_rejections for m in metrics)
    parts = [
        f"Cost ${result.total_cost_usd:.2f}",
        f"{total_input:,} input tokens ({cached:,} cached)",
        f"{output:,} output tokens",
        f"{turns} turns",
        f"{seconds:.1f} s",
        f"{subagents} subagents",
    ]
    if rejections:
        parts.append(f"{rejections} schema rejection{'s' if rejections != 1 else ''}")
    parts.append(f"session {result.session_id}")
    return " · ".join(parts)


def _meta(finding: Finding) -> str:
    return f"{finding.category} · {finding.status} · confidence {finding.confidence:.2f}"


def _details(finding: Finding) -> list[str]:
    lines = [finding.description.strip(), "", f"**Evidence:** {finding.evidence.strip()}"]
    if finding.suggested_fix:
        lines += ["", "**Suggested fix:**", "", "```", finding.suggested_fix.strip(), "```"]
    return lines
