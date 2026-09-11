import pytest
from pydantic import ValidationError

from argus.context.diff import DIFF_SIZE_CAP
from argus.settings import Settings


def test_defaults_match_the_spec() -> None:
    s = Settings(_env_file=None)

    assert s.lead_model == "claude-opus-5"
    assert s.lead_effort == "high"
    assert s.lead_max_turns == 40
    assert s.lead_max_budget_usd == 3.0
    assert s.specialist_model == "claude-sonnet-5"
    assert s.specialist_effort == "medium"
    assert s.specialist_max_turns == 15
    assert s.verifier_model == "claude-sonnet-5"
    assert s.verifier_effort == "medium"
    assert s.verifier_max_turns == 10
    assert s.verifier_max_budget_usd == 0.5
    assert s.verify_concurrency == 4
    assert s.diff_size_cap == DIFF_SIZE_CAP
    assert s.log_format == "auto"
    assert s.log_level == "info"


def test_every_sdk_effort_level_is_accepted() -> None:
    for effort in ("low", "medium", "high", "xhigh", "max"):
        assert Settings(_env_file=None, lead_effort=effort).lead_effort == effort


def test_argus_prefixed_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARGUS_LEAD_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("ARGUS_VERIFY_CONCURRENCY", "2")
    monkeypatch.setenv("ARGUS_LOG_FORMAT", "json")

    s = Settings(_env_file=None)

    assert s.lead_model == "claude-sonnet-5"
    assert s.verify_concurrency == 2
    assert s.log_format == "json"


def test_invalid_values_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARGUS_VERIFY_CONCURRENCY", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)

    monkeypatch.delenv("ARGUS_VERIFY_CONCURRENCY")
    monkeypatch.setenv("ARGUS_LEAD_EFFORT", "extreme")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
