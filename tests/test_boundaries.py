"""Architecture rules from CLAUDE.md, checked mechanically."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
PACKAGE = ROOT / "argus"
EVALS = ROOT / "evals"


def test_only_the_agent_package_imports_the_sdk() -> None:
    offenders = [
        path.relative_to(ROOT)
        for path in PACKAGE.rglob("*.py")
        if "claude_agent_sdk" in path.read_text() and path.relative_to(PACKAGE).parts[0] != "agent"
    ]

    assert offenders == []


def test_importing_the_non_agent_modules_does_not_load_the_sdk() -> None:
    modules = [
        "argus.domain.models",
        "argus.domain.errors",
        "argus.context.diff",
        "argus.context.git",
        "argus.settings",
        "argus.telemetry",
        "argus.tracing",
        "argus.cli",
        "evals.corpus",
        "evals.score",
        "evals.run",
    ]
    code = (
        "import sys, importlib\n"
        f"for m in {modules!r}: importlib.import_module(m)\n"
        "assert 'claude_agent_sdk' not in sys.modules, 'sdk loaded'\n"
    )

    subprocess.run([sys.executable, "-c", code], check=True, cwd=ROOT)


def test_the_evals_reach_the_sdk_only_through_the_agent_package() -> None:
    """The live runner uses SdkReviewAgent; nothing under evals/ names the SDK itself."""
    offenders = [
        path.relative_to(ROOT)
        for path in EVALS.rglob("*.py")
        if "claude_agent_sdk" in path.read_text() and "cases" not in path.relative_to(EVALS).parts
    ]

    assert offenders == []


def test_the_package_never_prints() -> None:
    offenders = [
        path.relative_to(ROOT)
        for path in [*PACKAGE.rglob("*.py"), *EVALS.rglob("*.py")]
        if any(line.lstrip().startswith("print(") for line in path.read_text().splitlines())
    ]

    assert offenders == []
