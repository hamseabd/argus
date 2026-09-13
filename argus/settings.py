"""Runtime settings, overridable through ARGUS_* environment variables.

Defaults are the v1 values, chosen on measured runs (see the README's Cost
section). Everything that shapes a query (model, effort, caps) lives here
so the agent layer builds options from one object and tests can pin any
value without touching the environment.
"""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from argus.context.diff import DIFF_SIZE_CAP

Effort = Literal["low", "medium", "high", "xhigh", "max"]
LogFormat = Literal["auto", "json", "console"]
LogLevel = Literal["debug", "info", "warning"]


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

    specialist_model: str = "claude-sonnet-5"
    specialist_effort: Effort = "medium"
    specialist_max_turns: int = Field(default=25, ge=1)

    verifier_model: str = "claude-sonnet-5"
    verifier_effort: Effort = "medium"
    verifier_max_turns: int = Field(default=10, ge=1)
    verifier_max_budget_usd: float = Field(default=0.5, gt=0)
    verify_concurrency: int = Field(default=4, ge=1)

    diff_size_cap: int = Field(default=DIFF_SIZE_CAP, ge=1)
    log_format: LogFormat = "auto"
    log_level: LogLevel = "info"
