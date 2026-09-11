import io

import pytest

from argus import telemetry


@pytest.fixture(autouse=True)
def isolated_logging() -> None:
    """Every test starts with fresh, silent JSON logging and no bound run context."""
    telemetry.configure(log_format="json", stream=io.StringIO())
