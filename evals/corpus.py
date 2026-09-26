"""The seeded-bug corpus: cases on disk and the git repository each one becomes.

A case is a directory under evals/cases/ with a `base/` snapshot, a `seeded/`
snapshot, and `expected.yaml`, the bugs the seeded change introduces. The
repository layout is the one tests/helpers.build_seeded_repo uses: `main`
holds the base, a `feature` branch holds the seeded change, and the working
tree is left on `feature`, so `argus review --diff --base main` sees exactly
the change under test. Clean controls expect no findings.

No SDK imports here: this module is shared with the offline tests.
"""

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from argus.domain.models import Category, Severity

CASES_DIR = Path(__file__).parent / "cases"
CLEAN_PREFIX = "clean_"
"""Clean controls are named clean_*: a change with no bug, which should draw no finding."""


class ExpectedBug(BaseModel):
    """One seeded bug: where it is in the seeded version and how Argus should file it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    category: Category
    severity: Severity

    @model_validator(mode="after")
    def _end_follows_start(self) -> "ExpectedBug":
        if self.line_end < self.line_start:
            raise ValueError(f"line_end {self.line_end} precedes line_start {self.line_start}")
        return self


@dataclass(frozen=True)
class Case:
    name: str
    path: Path
    expected: tuple[ExpectedBug, ...]

    @property
    def clean(self) -> bool:
        return self.name.startswith(CLEAN_PREFIX)


def load_cases(root: Path = CASES_DIR) -> list[Case]:
    """Every case under root, sorted by name so reports are stable."""
    cases = []
    for path in sorted(p for p in root.iterdir() if p.is_dir()):
        raw = yaml.safe_load((path / "expected.yaml").read_text(encoding="utf-8")) or []
        try:
            expected = tuple(ExpectedBug.model_validate(item) for item in raw)
        except ValueError as exc:
            raise ValueError(f"{path.name}/expected.yaml: {exc}") from exc
        cases.append(Case(name=path.name, path=path, expected=expected))
    return cases


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def build_case_repo(case: Case, target: Path) -> Path:
    """A git repo whose main holds the base snapshot and whose feature branch holds the seeded one.

    The seeded snapshot replaces the base rather than overlaying it, so a case
    can delete or rename a file.
    """
    target.mkdir(parents=True, exist_ok=True)
    _git(target, "init", "-q", "-b", "main")
    _git(target, "config", "user.email", "fixture@argus.test")
    _git(target, "config", "user.name", "Fixture")
    _git(target, "config", "commit.gpgsign", "false")
    shutil.copytree(case.path / "base", target, dirs_exist_ok=True)
    _git(target, "add", "-A")
    _git(target, "commit", "-q", "-m", "feat: initial version")
    _git(target, "checkout", "-q", "-b", "feature")
    _git(target, "rm", "-rq", ".")
    shutil.copytree(case.path / "seeded", target, dirs_exist_ok=True)
    _git(target, "add", "-A")
    _git(target, "commit", "-q", "-m", "refactor: tidy up")
    return target
