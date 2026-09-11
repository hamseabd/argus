"""Architecture rules from CLAUDE.md, checked mechanically."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
PACKAGE = ROOT / "argus"


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
        "argus.cli",
    ]
    code = (
        "import sys, importlib\n"
        f"for m in {modules!r}: importlib.import_module(m)\n"
        "assert 'claude_agent_sdk' not in sys.modules, 'sdk loaded'\n"
    )

    subprocess.run([sys.executable, "-c", code], check=True, cwd=ROOT)


def test_no_print_outside_scripts() -> None:
    offenders = [
        path.relative_to(ROOT)
        for path in PACKAGE.rglob("*.py")
        if any(line.lstrip().startswith("print(") for line in path.read_text().splitlines())
    ]

    assert offenders == []
