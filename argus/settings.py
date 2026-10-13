"""Runtime settings, overridable through ARGUS_* environment variables.

Defaults are the v1 values, chosen on measured runs (see the README's Cost
section). Everything that shapes a query (model, effort, caps) lives here
so the agent layer builds options from one object and tests can pin any
value without touching the environment.
"""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from argus import __version__
from argus.context.diff import DIFF_SIZE_CAP
from argus.domain.models import RunConfig
from argus.telemetry import LogFormat, LogLevel

Effort = Literal["low", "medium", "high", "xhigh", "max"]

DEFAULT_ANSWER_HOLDS = 3
"""Times the lead's answer may be held in one query before it goes through regardless."""
DEFAULT_IGNORE_PATHS = "*.min.js,*.min.css"
"""Minified bundles: generated and unreadable as a diff.

Lockfiles are deliberately not here. They are large and tedious, but a
dependency substitution or a changed integrity hash hides in exactly one,
and a reviewer that skips them by default would miss it; add `*.lock` to
ARGUS_IGNORE_PATHS to opt out for a repository that reviews them elsewhere.
"""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARGUS_", extra="ignore")

    lead_model: str = "claude-opus-5"
    lead_effort: Effort = "high"
    lead_max_turns: int = Field(default=40, ge=1)
    lead_read_budget: int = Field(
        default=10,
        ge=0,
        description="Reads the lead may make itself before it must answer; the specialists read.",
    )
    lead_max_budget_usd: float = Field(default=3.0, gt=0)
    lead_max_answer_holds: int = Field(
        default=DEFAULT_ANSWER_HOLDS,
        ge=0,
        description="Times the lead's answer may be held for a specialist still reporting.",
    )

    specialist_model: str = "claude-sonnet-5"
    specialist_effort: Effort = "medium"
    specialist_max_turns: int = Field(default=25, ge=1)

    verifier_model: str = "claude-sonnet-5"
    verifier_effort: Effort = "medium"
    verifier_max_turns: int = Field(default=10, ge=1)
    verifier_max_budget_usd: float = Field(default=0.5, gt=0)
    verify_concurrency: int = Field(default=4, ge=1)

    diff_size_cap: int = Field(default=DIFF_SIZE_CAP, ge=1)
    ignore_paths: str = Field(
        default=DEFAULT_IGNORE_PATHS,
        description=(
            "Comma-separated patterns for changed files to leave out of the review: "
            "a file name or glob (*.min.js) in any directory, a directory at the repository "
            "root (vendor/), or a path glob (docs/*.md). Empty turns the filter off."
        ),
    )

    @property
    def ignore_patterns(self) -> tuple[str, ...]:
        return tuple(p.strip() for p in self.ignore_paths.split(",") if p.strip())

    reviewer_login: str = Field(
        default="",
        description=(
            "The GitHub login Argus posts reviews as (the app slug plus [bot]). Only that "
            "login's earlier reviews are read back as known findings; unset, none are."
        ),
    )
    log_format: LogFormat = "auto"
    log_level: LogLevel = "info"
    trace_content: bool = Field(
        default=False,
        description="Put prompts, structured output, and model text on trace spans.",
    )

    def run_config(self, prompts_sha: str) -> RunConfig:
        """Everything here that shapes a query, plus the prompts, as the record of one run.

        Logging and tracing settings are left out: they change what is reported,
        not what the models are asked.
        """
        return RunConfig(
            argus_version=__version__,
            prompts_sha=prompts_sha,
            lead_model=self.lead_model,
            lead_effort=self.lead_effort,
            lead_max_turns=self.lead_max_turns,
            lead_read_budget=self.lead_read_budget,
            lead_max_budget_usd=self.lead_max_budget_usd,
            lead_max_answer_holds=self.lead_max_answer_holds,
            specialist_model=self.specialist_model,
            specialist_effort=self.specialist_effort,
            specialist_max_turns=self.specialist_max_turns,
            verifier_model=self.verifier_model,
            verifier_effort=self.verifier_effort,
            verifier_max_turns=self.verifier_max_turns,
            verifier_max_budget_usd=self.verifier_max_budget_usd,
            verify_concurrency=self.verify_concurrency,
            diff_size_cap=self.diff_size_cap,
        )
