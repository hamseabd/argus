"""Findings recorded one at a time through a tool, instead of re-typed by the lead.

Each specialist calls report_finding once per finding. The call is validated
against the domain model on the spot, and against the repository: the file
must exist inside it and the line must be in the file. A bad finding is
refused with the reason, so the specialist fixes it while it still has the
code in front of it, rather than the lead's whole answer failing later.

What is recorded is what the review is built from. The lead never copies a
finding into its answer; it names the ids to drop, so a finding cannot be
lost or altered on its way from the specialist to the verifier, and the most
expensive model writes a few ids instead of every finding again.

The tool handler cannot see which agent called it; the PostToolUse hook can,
and attributes each id it sees answered (see argus.agent.hooks).
"""

import re
from itertools import islice
from pathlib import Path
from typing import Any

from claude_agent_sdk import SdkMcpTool, ToolAnnotations, tool
from pydantic import ValidationError

from argus.agent.schemas import PIPELINE_OWNED, PROSE_MIN_LENGTH, finding_input_schema
from argus.context.source import within_repository
from argus.domain.models import MAX_FINDINGS, Finding

REPORT_TOOL = "report_finding"
MAX_RECORDED = MAX_FINDINGS * 2
"""Findings one review may record before the tool refuses more; the lead drops duplicates after."""
RECORDED_ID = re.compile(r"\bRecorded (F\d+)\b")
"""How the tool's answer names the id, and how the hook reads it back."""


class FindingCollector:
    """The findings one review stage recorded, in order, keyed F1, F2, ..."""

    def __init__(self, repo_root: Path) -> None:
        self._root = repo_root.resolve()
        self._findings: list[Finding] = []
        self._reporters: dict[str, str] = {}

    def add(self, raw: dict[str, Any]) -> str:
        """Validate and record one finding; returns its id or raises ValueError with the reason."""
        finding = self._validated(raw, f"F{len(self._findings) + 1}")
        self._findings.append(finding)
        return finding.id

    def problem(self, raw: dict[str, Any]) -> str | None:
        """Why report_finding would refuse raw, said so the model can fix it; None if it would not.

        Checked before the call reaches the tool: the SDK validates it against
        the schema first and answers a missing field with only its name, so this
        names every missing field and what it must hold, in one answer.
        """
        schema = finding_input_schema()
        if missing := [name for name in schema["required"] if name not in raw]:
            fields = "; ".join(_requirement(name, schema["properties"][name]) for name in missing)
            return (
                f"missing {fields}. Call report_finding again with every field: "
                "give null for a field that has no value, never leave one out."
            )
        try:
            self._validated(raw, "F0")  # a placeholder id: this only checks, add() records
        except ValueError as exc:
            return str(exc)
        return None

    def _validated(self, raw: dict[str, Any], finding_id: str) -> Finding:
        if owned := sorted(PIPELINE_OWNED[Finding] & set(raw)):
            raise ValueError(f"{', '.join(owned)} are filled in by Argus; leave them out")
        if len(self._findings) >= MAX_RECORDED:
            raise ValueError(
                f"{MAX_RECORDED} findings are already recorded for this review and no more "
                "can be added; finish with what is recorded"
            )
        for name, floor in PROSE_MIN_LENGTH[Finding].items():
            if len(str(raw.get(name) or "").strip()) < floor:
                raise ValueError(
                    f"{name} must be at least {floor} characters: the real reasoning, "
                    "not a placeholder"
                )
        try:
            # Strict, as the SDK's schema check is: a line of "3" is refused here too,
            # so the hook never passes a call the SDK then refuses unseen.
            finding = Finding.model_validate({**raw, "id": finding_id}, strict=True)
        except ValidationError as exc:
            raise ValueError(_explain(exc)) from exc
        self._check_location(finding)
        return finding

    def attribute(self, finding_id: str, agent: str) -> None:
        """Record which agent reported finding_id; the first attribution stands."""
        self._reporters.setdefault(finding_id, agent)

    def findings(self) -> list[Finding]:
        """Every recorded finding, with its reporter where one was attributed."""
        return [
            f.model_copy(update={"reported_by": self._reporters.get(f.id)}) for f in self._findings
        ]

    def _check_location(self, finding: Finding) -> None:
        target = (self._root / finding.file).resolve()
        if not within_repository(self._root, target):
            raise ValueError(f"{finding.file} is outside the repository or inside its .git")
        if not target.is_file():
            raise ValueError(
                f"{finding.file} does not exist; use the repository-relative path from the diff"
            )
        last = finding.end_line or finding.line
        with target.open(encoding="utf-8", errors="replace") as handle:
            lines = sum(1 for _ in islice(handle, last))  # enough to know the range fits
        if last > lines:
            raise ValueError(
                f"{finding.file} has {lines} lines; line {last} is past its end. "
                "Use the line number in the new version of the file."
            )


def report_finding_tool(collector: FindingCollector) -> SdkMcpTool[Any]:
    @tool(
        REPORT_TOOL,
        (
            "Record one finding for this review. Call it once per finding, as soon as the "
            "finding is supported. It is validated on the spot: on success it answers with "
            "the finding's id (F1, F2, ...); on a problem it says what to fix, so fix it and "
            "call again. `line` is the line in the new version of the file."
        ),
        finding_input_schema(),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            finding_id = collector.add(args)
        except ValueError as exc:
            return {"content": [{"type": "text", "text": f"Not recorded: {exc}"}], "is_error": True}
        return {"content": [{"type": "text", "text": f"Recorded {finding_id}."}]}

    return handler


_TYPE_WORDS = {"string": "a string", "integer": "an integer", "number": "a number"}


def _requirement(name: str, prop: dict[str, Any]) -> str:
    """One field of the report_finding schema, said as what it must hold."""
    variants = prop.get("anyOf", [prop])
    value = next(v for v in variants if v.get("type") != "null")
    parts: list[str] = []
    if description := prop.get("description") or value.get("description"):
        parts.append(description.rstrip("."))
    if "enum" in value:
        parts.append("one of " + ", ".join(value["enum"]))
    else:
        parts.append(_TYPE_WORDS.get(value.get("type", ""), str(value.get("type"))))
    if "minLength" in value:
        parts.append(f"at least {value['minLength']} characters")
    if "maxLength" in value:
        parts.append(f"at most {value['maxLength']} characters")
    if "minimum" in value and "maximum" in value:
        parts.append(f"from {value['minimum']} to {value['maximum']}")
    elif "minimum" in value:
        parts.append(f"at least {value['minimum']}")
    if len(variants) > 1:
        parts.append("or null when there is none")
    return f"{name} ({'; '.join(parts)})"


def _explain(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or 'finding'}: {err['msg']}" for err in exc.errors()
    )
