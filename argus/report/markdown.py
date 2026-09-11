"""Markdown rendering shared by the terminal report and the GitHub review."""

from argus.domain.models import AgentMetrics, Finding, ReviewResult, rank_findings


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
    footer = " · ".join(parts)
    review_stage = next((m for m in metrics if m.stage == "review"), None)
    if review_stage and review_stage.agents:
        footer += "\n" + render_agents(review_stage.agents)
    return footer


def render_agents(agents: list[AgentMetrics]) -> str:
    """One line: what the lead and each specialist did during the review stage."""
    return "Agents: " + " · ".join(_agent_summary(a) for a in agents)


def _agent_summary(agent: AgentMetrics) -> str:
    parts = [
        f"{agent.agent} {agent.turns} {_plural(agent.turns, 'turn')}",
        f"{agent.tool_calls} {_plural(agent.tool_calls, 'tool call')}",
    ]
    if agent.duration_ms:
        parts.append(f"{agent.duration_ms / 1000:.1f} s")
    return ", ".join(parts)


def _plural(count: int, noun: str) -> str:
    return noun if count == 1 else noun + "s"


def _meta(finding: Finding) -> str:
    return f"{finding.category} · {finding.status} · confidence {finding.confidence:.2f}"


def _details(finding: Finding) -> list[str]:
    lines = [finding.description.strip(), "", f"**Evidence:** {finding.evidence.strip()}"]
    if finding.suggested_fix:
        lines += ["", "**Suggested fix:**", "", "```", finding.suggested_fix.strip(), "```"]
    return lines
