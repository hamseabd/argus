"""Prompt templates and the user prompts built from a review context.

The system prompts are Markdown files next to this module so they can be
read and edited as prose. The functions here assemble the per-run user
prompts: what the lead is asked to review, and what the verifier is asked
to check.
"""

from importlib import resources

from argus.domain.models import ChangedFile, Finding, ReviewContext

PROMPT_NAMES: tuple[str, ...] = ("lead", "correctness", "security", "quality", "verifier")


def load_prompt(name: str) -> str:
    if name not in PROMPT_NAMES:
        raise ValueError(f"unknown prompt {name!r}; expected one of {PROMPT_NAMES}")
    return resources.files(__package__).joinpath(f"{name}.md").read_text(encoding="utf-8")


def lead_user_prompt(context: ReviewContext) -> str:
    parts: list[str] = []
    if context.pr is not None:
        parts.append(f"# Pull request #{context.pr.number}: {context.pr.title}")
        if context.pr.body.strip():
            parts.append(context.pr.body.strip())
    else:
        parts.append("# Local change")
    parts.append("## Changed files\n\n" + "\n".join(f"- {_describe(f)}" for f in context.files))
    if context.truncated_files:
        parts.append(
            "## Files omitted from the diff because of its size\n\n"
            "Read these with the Read tool if they matter to the review:\n\n"
            + "\n".join(f"- {path}" for path in context.truncated_files)
        )
    parts.append("## Diff\n\n```diff\n" + context.diff_text.rstrip("\n") + "\n```")
    return "\n\n".join(parts) + "\n"


def verifier_user_prompt(finding: Finding, diff_section: str) -> str:
    lines = [
        "# Finding to verify",
        "",
        f"- Location: {finding.location}",
        f"- Severity: {finding.severity}",
        f"- Category: {finding.category}",
        f"- Title: {finding.title}",
        f"- Confidence claimed by the reporter: {finding.confidence:.2f}",
        "",
        "## Description",
        "",
        finding.description,
        "",
        "## Evidence given",
        "",
        finding.evidence,
    ]
    if finding.suggested_fix:
        lines += ["", "## Suggested fix", "", finding.suggested_fix]
    if diff_section:
        lines += ["", "## Diff for this file", "", "```diff", diff_section.rstrip("\n"), "```"]
    else:
        lines += ["", "The file is not in the diff; read it directly."]
    return "\n".join(lines) + "\n"


def _describe(file: ChangedFile) -> str:
    if file.status == "renamed" and file.previous_path:
        return f"{file.path} (renamed from {file.previous_path})"
    return f"{file.path} ({file.status})"
