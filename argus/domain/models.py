"""Domain models shared by every pipeline stage.

These are plain Pydantic v2 models with no knowledge of the Agent SDK.
The review stage produces a Review, the verify stage produces one Verdict
per finding, and the pipeline folds both into a ReviewResult.

Findings are frozen: a status change produces a new Finding via
with_status(), so a stage can never mutate another stage's output.
"""

from collections import Counter
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Severity = Literal["critical", "high", "medium", "low"]
Category = Literal["correctness", "security", "quality"]
Status = Literal["pending", "confirmed", "rejected", "unverified"]

SEVERITY_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_STATUS_ORDER: dict[str, int] = {"confirmed": 0, "unverified": 1, "pending": 2}

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
    confidence: float = Field(ge=0.0, le=1.0)
    status: Status = "pending"

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


class Verdict(_Model):
    """The verifier's judgement on one finding."""

    finding_id: str
    verdict: Literal["confirmed", "rejected"]
    reasoning: str
    confidence: float = Field(ge=0.0, le=1.0)


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


class ReviewResult(_Model):
    """Everything a run produced, serialised as the JSON artifact."""

    review: Review = Field(description="Findings carry their final status.")
    verdicts: list[Verdict]
    metrics: list[StageMetrics]
    total_cost_usd: float = Field(ge=0.0, description="Every query, failed ones included.")
    duration_ms: int = Field(default=0, ge=0, description="Wall-clock time of the whole run.")
    session_id: str


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
    pr: PRInfo | None = None


def rank_findings(findings: list[Finding]) -> list[Finding]:
    """Order findings for the report and drop the rejected ones.

    Confirmed findings come before unverified ones, then by severity from
    critical to low, then by file path and line so the order is stable.
    """
    kept = [f for f in findings if f.status != "rejected"]
    return sorted(
        kept,
        key=lambda f: (_STATUS_ORDER[f.status], SEVERITY_ORDER[f.severity], f.file, f.line),
    )
