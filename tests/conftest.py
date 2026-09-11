import io

import pytest

from argus import telemetry


@pytest.fixture(autouse=True)
def isolated_logging() -> None:
    """Every test starts with fresh, silent JSON logging and no bound run context."""
    telemetry.configure(log_format="json", stream=io.StringIO())


@pytest.fixture(autouse=True)
def plain_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI output without color or terminal-dependent wrapping.

    Typer renders errors through rich, which on a color terminal splits words
    like `--diff` into separately styled runs; assertions on the text would
    then depend on the runner's TERM and FORCE_COLOR.
    """
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
