"""Domain models shared by every pipeline stage.

These are plain Pydantic v2 models with no knowledge of the Agent SDK.
The review stage produces a Review, the verify stage produces one Verdict
per finding, and the pipeline folds both into a ReviewResult.

Findings are frozen: a status change produces a new Finding via
with_status(), so a stage can never mutate another stage's output.
"""

import hashlib
import json
import re
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Severity = Literal["critical", "high", "medium", "low"]
Category = Literal["correctness", "security", "quality"]
Status = Literal["pending", "confirmed", "rejected", "unverified", "known"]
"""known: an earlier Argus review on the same pull request already reported it."""

SEVERITY_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_STATUS_ORDER: dict[str, int] = {"confirmed": 0, "unverified": 1, "pending": 2}
HIDDEN_STATUSES: frozenset[str] = frozenset({"rejected", "known"})
"""Statuses a report counts but never shows: refuted, or already on the pull request."""
KNOWN_LINE_TOLERANCE = 2
"""How far a finding may sit from an earlier one on the same file and still be the same claim."""

MAX_FINDINGS = 25
MAX_TITLE_LENGTH = 100


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Finding(_Model):
    """One defect a reviewer claims to have found."""

    id: str = Field(description="Pipeline-assigned, e.g. security-2.")
    file: str = Field(description="Repository-relative path.")
    line: int = Field(ge=1, description="Line number in the new version of the file.")
    end_line: int | None = Field(default=None, ge=1)
    severity: Severity
    category: Category
    title: str = Field(max_length=MAX_TITLE_LENGTH)
    description: str = Field(description="What is wrong and why it matters.")
    evidence: str = Field(description="The code path or reasoning that supports the claim.")
    suggested_fix: str | None = None
    replacement: str | None = Field(
        default=None,
        description=(
            "The exact new text of lines line..end_line, offered as a one-click suggestion. "
            "Only for a fix confined to those lines; empty deletes them."
        ),
    )
    confidence: float = Field(ge=0.0, le=1.0)
    status: Status = "pending"
    reported_by: str | None = Field(
        default=None, description='Pipeline-assigned: the agent that reported it, e.g. "security".'
    )

    @model_validator(mode="after")
    def _end_line_follows_line(self) -> "Finding":
        if self.end_line is not None and self.end_line < self.line:
            raise ValueError(f"end_line {self.end_line} precedes line {self.line}")
        return self

    def with_status(self, status: Status) -> "Finding":
        return self.model_copy(update={"status": status})

    @property
    def location(self) -> str:
        """`path:line` or `path:start-end`, as reports and prompts show it."""
        if self.end_line is not None and self.end_line != self.line:
            return f"{self.file}:{self.line}-{self.end_line}"
        return f"{self.file}:{self.line}"


class KnownFinding(_Model):
    """A finding an earlier Argus review already posted on the pull request."""

    file: str
    line: int | None = Field(
        default=None, description="None when the comment sat in the review body or has no line."
    )
    title: str
    severity: Severity | None = Field(default=None, description="As posted; None if unreadable.")
    category: Category | None = Field(default=None, description="As posted; None if unreadable.")
    url: str = Field(description="Where it was posted, so a skipped finding can be found.")


def matches_known(finding: "Finding", known: Sequence["KnownFinding"]) -> "KnownFinding | None":
    """The earlier finding this one repeats, or None.

    Same file and the same title, however spaced, cased, or punctuated, is the
    same claim even when the code has moved. Same file within a couple of lines
    is the same claim only when the kind and the weight agree too: a reworded
    title for the same category and severity, never a low nit nearby swallowing
    a new critical finding.
    """
    title = _normalized(finding.title)
    for earlier in known:
        if earlier.file != finding.file:
            continue
        if _normalized(earlier.title) == title:
            return earlier
        if (
            earlier.line is not None
            and abs(earlier.line - finding.line) <= KNOWN_LINE_TOLERANCE
            and earlier.severity == finding.severity
            and earlier.category == finding.category
        ):
            return earlier
    return None


_PUNCTUATION = re.compile(r"[^\w\s]")


def _normalized(title: str) -> str:
    """Lowercase, punctuation anywhere dropped, whitespace collapsed."""
    return " ".join(_PUNCTUATION.sub(" ", title.lower()).split())


class Review(_Model):
    """The lead reviewer's merged output."""

    summary: str = Field(description="Two to five sentences.")
    findings: list[Finding] = Field(max_length=MAX_FINDINGS)
    files_reviewed: list[str]

    @model_validator(mode="before")
    @classmethod
    def _assign_missing_ids(cls, data: Any) -> Any:
        """Number findings per category (correctness-1, security-1, correctness-2, ...).

        The model returns findings without ids; ids are pipeline-owned so the
        verify stage and the report can refer to a finding unambiguously.
        Findings that already carry an id keep it.
        """
        if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
            return data
        counters: Counter[str] = Counter()
        findings = []
        for item in data["findings"]:
            if isinstance(item, dict) and not item.get("id"):
                category = str(item.get("category", "finding"))
                counters[category] += 1
                item = {**item, "id": f"{category}-{counters[category]}"}
            findings.append(item)
        return {**data, "findings": findings}

    @field_validator("findings")
    @classmethod
    def _ids_are_unique(cls, findings: list[Finding]) -> list[Finding]:
        duplicates = [fid for fid, n in Counter(f.id for f in findings).items() if n > 1]
        if duplicates:
            raise ValueError(f"duplicate finding ids: {', '.join(sorted(duplicates))}")
        return findings


class DroppedFinding(_Model):
    """A recorded finding the lead leaves out of the review, and why."""

    id: str = Field(description="The id report_finding answered with, e.g. F3.")
    reason: str = Field(description="Why: a duplicate of which id, or which standard it fails.")


class Triage(_Model):
    """The lead reviewer's answer: the specialists record findings, the lead triages them.

    Findings arrive through a tool, validated one at a time, so the lead never
    re-types them; it says what the change does and which recorded findings
    to drop, and every other one goes to the verifier.
    """

    summary: str = Field(description="Two to five sentences.")
    files_reviewed: list[str]
    dropped: list[DroppedFinding] = Field(
        description="Recorded findings to leave out: duplicates and ones below the standard."
    )

    @property
    def dropped_ids(self) -> set[str]:
        return {d.id for d in self.dropped}


class Verdict(_Model):
    """The verifier's judgement on one finding.

    inconclusive is an answer, not a failure: the verifier read the code and
    could not settle the claim from it. The finding stays unverified, so it is
    still reported, marked as such, instead of being dropped as rejected.
    """

    finding_id: str
    verdict: Literal["confirmed", "rejected", "inconclusive"]
    reasoning: str
    confidence: float = Field(ge=0.0, le=1.0)


class AgentMetrics(_Model):
    """One agent's share of a query: the lead thread or one subagent type."""

    agent: str = Field(description='"lead" or the subagent type, for example "security".')
    runs: int = Field(
        default=1, ge=1, description="Times this agent was started; the counts sum across runs."
    )
    turns: int = Field(default=0, ge=0, description="API turns the agent took.")
    tool_calls: int = Field(default=0, ge=0)
    tool_failures: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0, description="Subagents only: start to stop.")


class StageMetrics(_Model):
    """Cost and usage for one query."""

    stage: str = Field(description='"review" or "verify:<finding_id>".')
    model: str
    cost_usd: float = Field(ge=0.0)
    input_tokens: int = Field(ge=0, description="Uncached input tokens.")
    output_tokens: int = Field(ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)
    num_turns: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    subagents_run: int = Field(default=0, ge=0, description="Review stage only.")
    output_rejections: int = Field(
        default=0, ge=0, description="Structured outputs the SDK rejected before one validated."
    )
    answers_held: int = Field(
        default=0, ge=0, description="Times the lead's answer was held for a specialist."
    )
    structured_output_recovered: bool = Field(
        default=False,
        description=(
            "The query ended without an answer (plain text or the budget), so the last "
            "answer the SDK accepted during it was used."
        ),
    )
    agents: list[AgentMetrics] = Field(
        default_factory=list, description="Per-agent attribution; the lead comes first."
    )


FINGERPRINT_CHARS = 12
_UNFINGERPRINTED = frozenset({"fingerprint", "argus_version"})


class RunConfig(_Model):
    """What shaped a run, so two runs can be compared and one can be reproduced.

    The models, efforts, and caps come from the settings; prompts_sha digests
    the prompt files; fingerprint digests all of that together, so the same
    value on two runs means the same prompts and the same knobs. The Argus
    version is recorded but left out of the fingerprint: a release that
    changes neither should compare equal.
    """

    argus_version: str
    prompts_sha: str = Field(description="Digest of every prompt file, in a fixed order.")
    lead_model: str
    lead_effort: str
    lead_max_turns: int
    lead_read_budget: int
    lead_max_budget_usd: float
    lead_max_answer_holds: int
    specialist_model: str
    specialist_effort: str
    specialist_max_turns: int
    verifier_model: str
    verifier_effort: str
    verifier_max_turns: int
    verifier_max_budget_usd: float
    verify_concurrency: int
    diff_size_cap: int
    fingerprint: str = Field(
        default="",
        description="Digest of every field but argus_version; computed when not given.",
    )

    @model_validator(mode="before")
    @classmethod
    def _fill_fingerprint(cls, data: Any) -> Any:
        if not isinstance(data, dict) or data.get("fingerprint"):
            return data
        material = {k: v for k, v in data.items() if k not in _UNFINGERPRINTED}
        digest = hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode())
        return {**data, "fingerprint": digest.hexdigest()[:FINGERPRINT_CHARS]}


class ReviewResult(_Model):
    """Everything a run produced, serialised as the JSON artifact."""

    review: Review = Field(description="Findings carry their final status.")
    verdicts: list[Verdict]
    metrics: list[StageMetrics]
    total_cost_usd: float = Field(ge=0.0, description="Every query, failed ones included.")
    duration_ms: int = Field(default=0, ge=0, description="Wall-clock time of the whole run.")
    session_id: str
    config: RunConfig | None = Field(
        default=None, description="What shaped the run; None for a result built elsewhere."
    )


ChangedFileStatus = Literal["added", "modified", "removed", "renamed"]


class ChangedFile(_Model):
    path: str
    status: ChangedFileStatus
    previous_path: str | None = None


class PRInfo(_Model):
    owner: str
    repo: str
    number: int = Field(ge=1)
    title: str
    body: str
    base_ref: str
    head_ref: str
    head_sha: str
    html_url: str


class Increment(_Model):
    """What changed on a pull request since Argus last reviewed it.

    The review stage looks only at this; the pull request's whole diff stays
    in the context, because inline comments may only go on lines it shows.
    """

    since_sha: str = Field(description="The head commit the earlier Argus review covered.")
    diff_text: str
    files: list[ChangedFile]
    truncated_files: list[str] = Field(default_factory=list)


class ReviewContext(_Model):
    """What the review stage is given: the change and where to read the code."""

    source: Literal["pr", "local"]
    repo_root: Path
    diff_text: str
    files: list[ChangedFile]
    truncated_files: list[str] = Field(
        default_factory=list,
        description="Files omitted from diff_text because of the size cap.",
    )
    ignored_files: list[str] = Field(
        default_factory=list,
        description="Files left out of the review because they match an ignore pattern.",
    )
    pr: PRInfo | None = None
    increment: Increment | None = Field(
        default=None,
        description="Set on a re-review: only these changes are reviewed, not the whole diff.",
    )
    review_rules: str | None = Field(
        default=None,
        description="REVIEW.md from the base branch: the maintainers' rules for reviews here.",
    )


def rank_findings(findings: list[Finding]) -> list[Finding]:
    """Order findings for the report and drop the rejected ones.

    Rejected and known findings are dropped. Confirmed findings come before
    unverified ones, then by severity from critical to low, then by file path
    and line so the order is stable.
    """
    kept = [f for f in findings if f.status not in HIDDEN_STATUSES]
    return sorted(
        kept,
        key=lambda f: (_STATUS_ORDER[f.status], SEVERITY_ORDER[f.severity], f.file, f.line),
    )
