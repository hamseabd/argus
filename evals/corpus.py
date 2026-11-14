"""The seeded-bug corpus: cases on disk and the git repository each one becomes.

A case is a directory under evals/cases/ with a `base/` snapshot, a `seeded/`
snapshot, and `expected.yaml`, what the seeded change holds, plus an optional
`case.yaml` for a case that is reviewed under other settings. The repository
layout is the one tests/helpers.build_seeded_repo uses: `main` holds the base,
a `feature` branch holds the seeded change, and the working tree is left on
`feature`, so `argus review --diff --base main` sees exactly the change under
test.

A case is one of three kinds:

- bug: seeded defects, each owned by the specialist meant to find it;
- clean: a change with no defect, which should draw no finding;
- verifier-trap: a change with no defect but a plausible-looking one (the
  trap), which a specialist may well flag and the verifier should reject.

expected.yaml is either a bare list of bugs (a bug case, or a clean one when
the list is empty and the name starts with clean_) or a mapping with `kind`,
`bugs`, and `traps`.

No SDK imports here: this module is shared with the offline tests.
"""

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from argus.domain.errors import ArgusError
from argus.domain.models import Category, Severity

CASES_DIR = Path(__file__).parent / "cases"
CLEAN_PREFIX = "clean_"
"""A bare-list case named clean_* with no bugs is a clean control."""
CaseKind = Literal["bug", "clean", "verifier-trap"]


class Location(BaseModel):
    """A file and a line span in the seeded version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)

    @model_validator(mode="after")
    def _end_follows_start(self) -> "Location":
        if self.line_end < self.line_start:
            raise ValueError(f"line_end {self.line_end} precedes line_start {self.line_start}")
        return self


class ExpectedBug(Location):
    """One seeded bug: where it is in the seeded version and how Argus should file it.

    `also` lists other places a reviewer may rightly report the same bug, such
    as the unchanged caller a changed contract breaks. Only the main location
    must be in the diff; any of them matches, and the bug still counts once.
    """

    category: Category
    severity: Severity
    specialist: Category | None = None
    """The specialist meant to find it; None means the one its category names."""
    also: tuple[Location, ...] = ()

    @property
    def owner(self) -> Category:
        return self.specialist or self.category

    @property
    def locations(self) -> tuple[Location, ...]:
        return (
            Location(file=self.file, line_start=self.line_start, line_end=self.line_end),
            *self.also,
        )


class Trap(Location):
    """A plausible-looking non-bug, and the category a specialist would wrongly file it under."""

    category: Category


class Expected(BaseModel):
    """expected.yaml: the case's kind and what it holds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: CaseKind
    bugs: tuple[ExpectedBug, ...] = ()
    traps: tuple[Trap, ...] = ()

    @model_validator(mode="after")
    def _contents_fit_the_kind(self) -> "Expected":
        if self.kind == "clean" and (self.bugs or self.traps):
            raise ValueError("a clean case expects neither bugs nor traps")
        if self.kind == "bug" and (not self.bugs or self.traps):
            raise ValueError("a bug case expects at least one bug and no traps")
        if self.kind == "verifier-trap" and (not self.traps or self.bugs):
            raise ValueError("a verifier-trap case expects at least one trap and no bugs")
        return self


class CaseConfig(BaseModel):
    """case.yaml, optional: how this case is reviewed when it differs from the default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    diff_size_cap: int | None = Field(default=None, ge=1)


@dataclass(frozen=True)
class Case:
    name: str
    path: Path
    expected: tuple[ExpectedBug, ...]
    diff_size_cap: int | None = None
    """The diff budget this case is reviewed under; None is Argus's default."""
    kind: CaseKind = "bug"
    traps: tuple[Trap, ...] = ()

    @property
    def clean(self) -> bool:
        return self.kind == "clean"


def load_cases(root: Path = CASES_DIR) -> list[Case]:
    """Every case under root, sorted by name so reports are stable."""
    cases = []
    for path in sorted(p for p in root.iterdir() if p.is_dir()):
        raw = yaml.safe_load((path / "expected.yaml").read_text(encoding="utf-8")) or []
        try:
            expected = _expected(path.name, raw)
        except ValueError as exc:
            raise ValueError(f"{path.name}/expected.yaml: {exc}") from exc
        config_file = path / "case.yaml"
        raw_config = (
            yaml.safe_load(config_file.read_text(encoding="utf-8")) if config_file.exists() else {}
        )
        try:
            config = CaseConfig.model_validate(raw_config or {})
        except ValueError as exc:
            raise ValueError(f"{path.name}/case.yaml: {exc}") from exc
        cases.append(
            Case(
                name=path.name,
                path=path,
                expected=expected.bugs,
                diff_size_cap=config.diff_size_cap,
                kind=expected.kind,
                traps=expected.traps,
            )
        )
    return cases


def _expected(name: str, raw: object) -> Expected:
    """The mapping form as written, or a bare list of bugs with its kind read from the name."""
    if isinstance(raw, list):
        kind = "clean" if not raw and name.startswith(CLEAN_PREFIX) else "bug"
        return Expected.model_validate({"kind": kind, "bugs": raw})
    return Expected.model_validate(raw)


class CaseBuildError(ArgusError):
    """A case's repository could not be built: a git command or a snapshot copy failed."""


def _git(repo: Path, *args: str) -> None:
    try:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        output = (exc.stderr or exc.stdout or "").strip()
        raise CaseBuildError(f"git {' '.join(args)} failed: {output}") from exc


def build_case_repo(case: Case, target: Path) -> Path:
    """A git repo whose main holds the base snapshot and whose feature branch holds the seeded one.

    The seeded snapshot replaces the base rather than overlaying it, so a case
    can delete or rename a file. Any failure is a CaseBuildError, an ArgusError,
    so the runner scores the case as failed and moves on.
    """
    try:
        return _build(case, target)
    except OSError as exc:
        raise CaseBuildError(f"case {case.name}: {exc}") from exc


def _build(case: Case, target: Path) -> Path:
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
