# Argus Increments 0 and 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the Argus repository with CI and branch protection (increment 0), then prove the Claude Agent SDK runs headless on the subscription token and reports cost (increment 1), which is the go/no-go gate for the rest of the project.

**Architecture:** Increment 0 is tooling only: a public GitHub repo, `uv`-managed Python 3.12 project, ruff, pytest, a `ci` workflow, and a Typer CLI with a `version` command. Increment 1 adds `argus/auth.py` (credential detection), a smoke script that runs one real `query()`, and a `workflow_dispatch` workflow that runs it in Actions with only `CLAUDE_CODE_OAUTH_TOKEN`. Later increments (2 to 7) get their own plan once this gate passes, because their exact SDK calls are confirmed here.

**Tech Stack:** Python 3.12, uv, hatchling, Typer, pytest, ruff, claude-agent-sdk 0.2.152, GitHub Actions (`actions/checkout@v7`, `astral-sh/setup-uv@v10`), gh CLI.

**Spec:** `docs/superpowers/specs/2026-09-10-argus-design.md` (sections 2, 4, 10, 11, 12, 19)

## Global Constraints

- Python `>=3.12`; `uv` manages the environment and `uv.lock` is committed.
- Cost to operate is $0: public repo, no cloud resources, Claude auth via `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`, never an API key.
- `main` is protected: PR required, status check `lint-and-test` required, no direct pushes, no force pushes.
- One branch, one PR, one squash-merge per increment. Hamse reviews and merges every PR. Do not start the next increment until the previous PR is merged.
- Commit subjects: `type(scope): imperative summary`, under 72 chars, body explains why. No `Co-Authored-By` or agent-name trailers, per Hamse's global git rules.
- Long Markdown files: one sentence per line.
- Every increment's PR body pastes the verification output: ruff check, ruff format check, pytest count, and the increment's acceptance check.
- TDD for every behavior: failing test, minimal code, passing test, commit.

---

## File Structure

Increment 0 creates:

| Path | Responsibility |
|------|----------------|
| `.python-version` | pins `3.12` for uv |
| `pyproject.toml` | project metadata, dependencies, ruff and pytest config, `argus` script entry point |
| `uv.lock` | exact resolved dependency versions |
| `.gitignore` | Python, venv, tool caches, local secrets, Argus output |
| `LICENSE` | MIT |
| `README.md` | stub with CI badge and a link to the spec |
| `CLAUDE.md` | project brief for future agent sessions |
| `argus/__init__.py` | `__version__` |
| `argus/cli.py` | Typer app, `version` command |
| `tests/__init__.py` | makes `tests` a package |
| `tests/test_cli.py` | `argus version` test |
| `.github/workflows/ci.yml` | `lint-and-test` job on push to main and every PR |

Increment 1 creates:

| Path | Responsibility |
|------|----------------|
| `argus/auth.py` | `credential_source()`: which env var supplies Claude credentials |
| `tests/test_auth.py` | tests for `credential_source()` |
| `scripts/sdk_smoke.py` | one real `query()`, prints a JSON metrics line, exit 0 only on success |
| `.github/workflows/smoke.yml` | `workflow_dispatch` job matrix over two models, uses the subscription secret |

---

## Increment 0: `chore/scaffold`

### Task 1: Bootstrap `main` and create the GitHub repo

**Files:**
- Commit: `docs/superpowers/specs/2026-09-10-argus-design.md` (already on disk)
- Commit: `docs/superpowers/plans/2026-09-10-argus-increments-0-1.md` (this file)

**Interfaces:**
- Consumes: nothing
- Produces: remote `origin` at `github.com/hamseabd/argus`, protected `main` requiring status check `lint-and-test`

This is the only time anything lands on `main` without a PR.
An empty repo has nothing to open a PR against, so the bootstrap commit carries only the docs, and branch protection is applied immediately after the push.

- [ ] **Step 1: Confirm the starting state**

Run:
```bash
cd /Users/hamseabdi/Desktop/Projects/argus && git status --short && git log --oneline 2>/dev/null | head -1; git branch --show-current
```
Expected: `?? docs/` listed, no commits, branch `main`.

- [ ] **Step 2: Commit the docs on `main`**

Run:
```bash
cd /Users/hamseabdi/Desktop/Projects/argus && git add docs && git commit -q -m "docs: add Argus design spec and first plan" -m "Argus is a code-review agent on the Claude Agent SDK, built as a portfolio project. The spec records the approved design and the increment-by-increment delivery process; the plan covers increments 0 and 1." && git log --oneline
```
Expected: one commit listed.

- [ ] **Step 3: Create the public repo and push**

Run:
```bash
cd /Users/hamseabdi/Desktop/Projects/argus && gh repo create hamseabd/argus --public --source . --remote origin --push --description "Code-review agent on the Claude Agent SDK: parallel specialist subagents, self-verified findings, inline GitHub reviews. Runs for \$0 on GitHub Actions."
```
Expected: `https://github.com/hamseabd/argus` printed and `main` pushed.

- [ ] **Step 4: Squash-merge only, delete branches on merge**

Run:
```bash
gh repo edit hamseabd/argus --enable-squash-merge --enable-merge-commit=false --enable-rebase-merge=false --delete-branch-on-merge
```
Expected: no error.

- [ ] **Step 5: Protect `main`**

Run:
```bash
gh api -X PUT repos/hamseabd/argus/branches/main/protection --input - <<'JSON'
{
  "required_status_checks": { "strict": true, "contexts": ["lint-and-test"] },
  "enforce_admins": true,
  "required_pull_request_reviews": { "required_approving_review_count": 0 },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_linear_history": true
}
JSON
```
Expected: JSON response with `"enforce_admins": {"enabled": true}`.

- [ ] **Step 6: Verify protection**

Run:
```bash
gh api repos/hamseabd/argus/branches/main/protection -q '{contexts: .required_status_checks.contexts, admins: .enforce_admins.enabled, pr: .required_pull_request_reviews.required_approving_review_count, force: .allow_force_pushes.enabled}'
```
Expected: `{"contexts":["lint-and-test"],"admins":true,"pr":0,"force":false}`.

### Task 2: Project tooling on `chore/scaffold`

**Files:**
- Create: `.python-version`, `pyproject.toml`, `.gitignore`, `LICENSE`, `README.md`, `CLAUDE.md`, `argus/__init__.py`, `tests/__init__.py`
- Generated: `uv.lock`

**Interfaces:**
- Consumes: nothing
- Produces: `argus.__version__: str = "0.1.0"`; a working `uv run` environment with `typer`, `pytest`, `ruff`

- [ ] **Step 1: Install uv if missing and branch**

Run:
```bash
which uv || brew install uv; uv --version && cd /Users/hamseabdi/Desktop/Projects/argus && git checkout -q -b chore/scaffold && git branch --show-current
```
Expected: a uv version and `chore/scaffold`.

- [ ] **Step 2: Write `.python-version`**

```
3.12
```

- [ ] **Step 3: Write `pyproject.toml`**

```toml
[project]
name = "argus"
version = "0.1.0"
description = "Code-review agent on the Claude Agent SDK: parallel specialist subagents, self-verified findings, inline GitHub reviews"
readme = "README.md"
license = "MIT"
requires-python = ">=3.12"
authors = [{ name = "Hamse Abdi", email = "hmseabd@gmail.com" }]
dependencies = [
    "typer>=0.27",
]

[project.scripts]
argus = "argus.cli:app"

[dependency-groups]
dev = [
    "pytest>=8.4",
    "ruff>=0.13",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["argus"]

[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "N", "RUF"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-m 'not live'"
markers = [
    "live: runs the real Claude Agent SDK; opt in with `pytest -m live`",
]
```

- [ ] **Step 4: Write `.gitignore`**

```
# Python
__pycache__/
*.py[cod]
*.egg-info/
build/
dist/
.venv/
.pytest_cache/
.ruff_cache/
.coverage
htmlcov/

# Local config and secrets
.env
.env.*
.claude/settings.local.json

# Argus output
argus-review.json
```

- [ ] **Step 5: Write `LICENSE`**

```
MIT License

Copyright (c) 2026 Hamse Abdi

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- [ ] **Step 6: Write `README.md`**

```markdown
# Argus

[![CI](https://github.com/hamseabd/argus/actions/workflows/ci.yml/badge.svg)](https://github.com/hamseabd/argus/actions/workflows/ci.yml)

> A code-review agent on the Claude Agent SDK.
> A lead reviewer fans out to parallel specialist subagents, verifies every finding before reporting it, and posts inline GitHub reviews.
> Runs for $0 on GitHub Actions.

Status: under construction.
The approved design is in [docs/superpowers/specs/2026-09-10-argus-design.md](docs/superpowers/specs/2026-09-10-argus-design.md).
```

- [ ] **Step 7: Write `CLAUDE.md`**

````markdown
# CLAUDE.md - Argus

## What this is

Argus is a code-review agent built on the Claude Agent SDK (Python).
It reviews a PR or a local diff with a lead reviewer plus three parallel read-only specialist subagents, verifies each finding with a second query, and posts inline GitHub review comments.
It is a portfolio project: engineering quality and a readable architecture matter more than feature count.
The design lives in `docs/superpowers/specs/2026-09-10-argus-design.md`; read it before changing behavior.

## Stack

- Python 3.12, `uv` for environments and `uv.lock`, hatchling build.
- `claude-agent-sdk` for the agent loop, subagents, hooks, structured output, and one in-process MCP tool.
- Pydantic v2 domain models in `argus/domain/` with zero SDK imports.
- Typer CLI, structlog JSON logs, httpx for GitHub.
- pytest and ruff; GitHub Actions for CI and for dogfooding reviews.

## Hard rules

- Cost to operate is $0: no cloud resources, no API key. Claude auth is `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`.
- The agent is read-only. It never writes, edits, or runs code in the target repo. `allowed_tools` plus a `PreToolUse` deny hook enforce this.
- Only `argus/agent/` imports `claude_agent_sdk`.
- Structured logs, never `print`, outside of `scripts/`.
- One branch, one PR, one squash-merge per increment. Hamse merges. Verify before opening the PR and paste the evidence in the PR body.
- TDD: failing test first.

## Commands

```bash
uv sync                      # create .venv and install everything
uv run ruff check .          # lint
uv run ruff format --check . # format check
uv run pytest -q             # unit tests (live tests excluded)
uv run pytest -m live        # opt-in: real SDK against the seeded-bug fixture
uv run argus version
```
````

- [ ] **Step 8: Write `argus/__init__.py` and `tests/__init__.py`**

`argus/__init__.py`:
```python
"""Argus: a code-review agent on the Claude Agent SDK."""

__version__ = "0.1.0"
```

`tests/__init__.py`: empty file.

- [ ] **Step 9: Resolve and lock**

Run:
```bash
cd /Users/hamseabdi/Desktop/Projects/argus && uv sync && uv run python -c "import argus; print(argus.__version__)" && ls uv.lock
```
Expected: `0.1.0` and `uv.lock` present.

- [ ] **Step 10: Lint and format the empty project**

Run:
```bash
cd /Users/hamseabdi/Desktop/Projects/argus && uv run ruff check . && uv run ruff format --check .
```
Expected: `All checks passed!` and `N files already formatted`. If format reports changes, run `uv run ruff format .` and re-check.

- [ ] **Step 11: Commit**

```bash
cd /Users/hamseabdi/Desktop/Projects/argus && git add .python-version pyproject.toml uv.lock .gitignore LICENSE README.md CLAUDE.md argus/__init__.py tests/__init__.py && git commit -q -m "chore: scaffold project tooling" -m "uv-managed Python 3.12 project with hatchling, ruff, and pytest so every later increment lands on a locked, lintable, testable base. MIT license and a README stub pointing at the spec." && git log --oneline | head -2
```

### Task 3: `argus version` command

**Files:**
- Create: `argus/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `argus.__version__`
- Produces: `argus.cli.app: typer.Typer` with a `version` subcommand; later increments add `review` to the same app

- [ ] **Step 1: Write the failing test**

`tests/test_cli.py`:
```python
from typer.testing import CliRunner

from argus import __version__
from argus.cli import app

runner = CliRunner()


def test_version_prints_package_version() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.output.strip() == f"argus {__version__}"


def test_no_args_shows_help_and_fails() -> None:
    result = runner.invoke(app, [])

    assert result.exit_code != 0
    assert "version" in result.output
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /Users/hamseabdi/Desktop/Projects/argus && uv run pytest -q tests/test_cli.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'argus.cli'`.

- [ ] **Step 3: Write the minimal implementation**

`argus/cli.py`:
```python
"""Argus command line."""

import typer

from argus import __version__

app = typer.Typer(
    help="Argus: a code-review agent on the Claude Agent SDK.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """Argus command line."""


@app.command()
def version() -> None:
    """Print the Argus version."""
    typer.echo(f"argus {__version__}")
```

The empty `@app.callback()` keeps Typer in subcommand mode, so `argus version` stays a subcommand when `review` is added later.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /Users/hamseabdi/Desktop/Projects/argus && uv run pytest -q && uv run argus version`
Expected: `2 passed` and `argus 0.1.0`.

- [ ] **Step 5: Lint, then commit**

```bash
cd /Users/hamseabdi/Desktop/Projects/argus && uv run ruff check . && uv run ruff format --check . && git add argus/cli.py tests/test_cli.py && git commit -q -m "feat(cli): add version command" -m "Gives the scaffold a runnable entry point and a first test, so CI has something real to lint and run before the review pipeline exists." && git log --oneline | head -1
```

### Task 4: CI workflow, pull request, gate

**Files:**
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `pyproject.toml`, `uv.lock`
- Produces: status check named `lint-and-test`, which branch protection already requires

- [ ] **Step 1: Write `.github/workflows/ci.yml`**

```yaml
name: ci

on:
  push:
    branches: [main]
  pull_request:

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  lint-and-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v10
        with:
          enable-cache: true
      - name: Install
        run: uv sync --frozen
      - name: Lint
        run: uv run ruff check .
      - name: Format
        run: uv run ruff format --check .
      - name: Test
        run: uv run pytest -q
```

The job id `lint-and-test` is the status check context that branch protection requires.
`uv sync --frozen` installs exactly what `uv.lock` says and fails if the lock is stale.

- [ ] **Step 2: Run the full verification locally and keep the output**

Run:
```bash
cd /Users/hamseabdi/Desktop/Projects/argus && uv run ruff check . && uv run ruff format --check . && uv run pytest -q && uv run argus version
```
Expected: `All checks passed!`, `N files already formatted`, `2 passed`, `argus 0.1.0`. Copy this output into the PR body in Step 4.

- [ ] **Step 3: Commit and push**

```bash
cd /Users/hamseabdi/Desktop/Projects/argus && git add .github/workflows/ci.yml && git commit -q -m "ci: lint and test on push and pull request" -m "Branch protection requires the lint-and-test check, so every PR must pass ruff and pytest before Hamse can merge it." && git push -u origin chore/scaffold
```

- [ ] **Step 4: Open the PR with evidence**

````bash
cd /Users/hamseabdi/Desktop/Projects/argus && gh pr create --base main --head chore/scaffold --title "chore: scaffold project tooling and CI" --body "$(cat <<'EOF'
## Increment 0: scaffold

uv-managed Python 3.12 project, ruff, pytest, MIT license, README stub, CLAUDE.md, `argus version`, and a `lint-and-test` CI job that branch protection requires.

### Definition of done
- [x] CI green on this PR (`lint-and-test`)
- [x] `argus version` prints the version

### Verification (local)
```
<paste the Step 2 output here verbatim>
```
EOF
)"
````

- [ ] **Step 5: Wait for CI**

Run: `cd /Users/hamseabdi/Desktop/Projects/argus && gh pr checks --watch`
Expected: `lint-and-test` passes. If it fails, read `gh run view --log-failed`, fix on the branch, push, and re-watch.

- [ ] **Step 6: STOP for merge**

Report the PR URL and the CI result to Hamse.
Do not start increment 1 until the PR is merged.
After the merge: `git checkout main && git pull --ff-only && git branch -d chore/scaffold`.

---

## Increment 1: `feat/sdk-smoke`

### Task 5: Credential detection

**Files:**
- Create: `argus/auth.py`
- Test: `tests/test_auth.py`
- Modify: `pyproject.toml` (add `claude-agent-sdk` dependency), `uv.lock`

**Interfaces:**
- Consumes: nothing
- Produces: `argus.auth.CREDENTIAL_ENV_VARS: tuple[str, ...]` and `argus.auth.credential_source(env: Mapping[str, str] | None = None) -> str | None`. The CLI in a later increment calls this at startup and exits 1 when it returns `None`.

- [ ] **Step 0: Ask Hamse for the subscription secret (user action, in parallel)**

Hamse runs these once; the token is printed by the first command and pasted into the second:
```bash
claude setup-token
gh secret set CLAUDE_CODE_OAUTH_TOKEN -R hamseabd/argus
```
Task 7 needs the secret to exist. Continue with Steps 1 to 5 while waiting.

- [ ] **Step 1: Branch and add the SDK dependency**

Run:
```bash
cd /Users/hamseabdi/Desktop/Projects/argus && git checkout -q -b feat/sdk-smoke && uv add "claude-agent-sdk>=0.2.152" && uv run python -c "import claude_agent_sdk, importlib.metadata as m; print(m.version('claude-agent-sdk'))"
```
Expected: `0.2.152` or newer, and `pyproject.toml` plus `uv.lock` updated.

- [ ] **Step 2: Write the failing tests**

`tests/test_auth.py`:
```python
from argus.auth import CREDENTIAL_ENV_VARS, credential_source


def test_oauth_token_wins_over_api_key() -> None:
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "tok", "ANTHROPIC_API_KEY": "key"}

    assert credential_source(env) == "CLAUDE_CODE_OAUTH_TOKEN"


def test_api_key_alone_is_detected() -> None:
    assert credential_source({"ANTHROPIC_API_KEY": "key"}) == "ANTHROPIC_API_KEY"


def test_no_credentials_returns_none() -> None:
    assert credential_source({}) is None


def test_blank_values_do_not_count() -> None:
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "   ", "ANTHROPIC_API_KEY": ""}

    assert credential_source(env) is None


def test_default_env_is_process_environment(monkeypatch) -> None:
    for name in CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")

    assert credential_source() == "ANTHROPIC_API_KEY"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd /Users/hamseabdi/Desktop/Projects/argus && uv run pytest -q tests/test_auth.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'argus.auth'`.

- [ ] **Step 4: Write the minimal implementation**

`argus/auth.py`:
```python
"""Where Claude credentials come from.

Argus never reads credentials itself; the Claude Agent SDK does.
This module only reports which environment variable will supply them,
so the CLI can fail early with a useful message and the smoke script can
log which path it exercised.
"""

import os
from collections.abc import Mapping

CREDENTIAL_ENV_VARS: tuple[str, ...] = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


def credential_source(env: Mapping[str, str] | None = None) -> str | None:
    """Return the first credential variable with a non-blank value, or None.

    Order matters: the subscription token is checked first because it is the
    only path that keeps the project free to run.
    """
    source = os.environ if env is None else env
    for name in CREDENTIAL_ENV_VARS:
        if source.get(name, "").strip():
            return name
    return None
```

- [ ] **Step 5: Run the tests, lint, commit**

Run: `cd /Users/hamseabdi/Desktop/Projects/argus && uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: `7 passed`, clean lint and format.

```bash
cd /Users/hamseabdi/Desktop/Projects/argus && git add pyproject.toml uv.lock argus/auth.py tests/test_auth.py && git commit -q -m "feat(auth): detect which env var supplies Claude credentials" -m "The CLI needs to fail fast when no credentials are present, and the smoke test needs to log which path it exercised. The subscription token is checked first because it is the only path that keeps the project free." && git log --oneline | head -1
```

### Task 6: SDK smoke script and local run

**Files:**
- Create: `scripts/sdk_smoke.py`

**Interfaces:**
- Consumes: `argus.auth.credential_source`
- Produces: a script that exits 0 and prints one JSON line `{"ok": true, "model": ..., "subtype": "success", "cost_usd": ..., "num_turns": ..., "duration_ms": ..., "usage": ..., "session_id": ...}`

- [ ] **Step 1: Write `scripts/sdk_smoke.py`**

```python
"""Run one real query through the Claude Agent SDK and report what it cost.

This is the go/no-go check for Argus. It proves that the SDK runs headless
with whatever credentials the environment supplies (the subscription token
in CI), that the model id is accepted, and that cost and usage are reported.

Usage:
    uv run python scripts/sdk_smoke.py --model claude-sonnet-5

Prints one JSON line to stdout. Exit code 0 only when the result subtype is
"success" and the model answered with the expected token.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from argus.auth import credential_source

MAGIC = "ARGUS_OK"


async def run(model: str) -> int:
    source = credential_source()
    print(f"credential source: {source or 'none in env (machine login)'}", file=sys.stderr)
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=f"You are a smoke test. Reply with exactly {MAGIC} and nothing else.",
        allowed_tools=[],
        permission_mode="dontAsk",
        max_turns=1,
        max_budget_usd=0.10,
    )
    try:
        async for message in query(prompt="Reply now.", options=options):
            if isinstance(message, ResultMessage):
                ok = message.subtype == "success" and MAGIC in (message.result or "")
                print(
                    json.dumps(
                        {
                            "ok": ok,
                            "model": model,
                            "subtype": message.subtype,
                            "cost_usd": message.total_cost_usd,
                            "num_turns": message.num_turns,
                            "duration_ms": message.duration_ms,
                            "usage": message.usage,
                            "session_id": message.session_id,
                        }
                    )
                )
                return 0 if ok else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "model": model, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps({"ok": False, "model": model, "subtype": "no_result"}))
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--model", default="claude-sonnet-5", help="model id to exercise")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.model)))


if __name__ == "__main__":
    main()
```

`print` is allowed here because `scripts/` is exempt from the structured-logs rule in `CLAUDE.md`.

- [ ] **Step 2: Run it locally with the machine login**

Run:
```bash
cd /Users/hamseabdi/Desktop/Projects/argus && env -u CLAUDE_CODE_OAUTH_TOKEN -u ANTHROPIC_API_KEY uv run python scripts/sdk_smoke.py --model claude-sonnet-5; echo "exit=$?"
```
Expected: `credential source: none in env (machine login)` on stderr, a JSON line with `"ok": true`, `exit=0`.
If it fails on authentication, the bundled runtime does not share the machine login; rerun with the token exported: `CLAUDE_CODE_OAUTH_TOKEN=<token from claude setup-token> uv run python scripts/sdk_smoke.py --model claude-sonnet-5`.
Record which of the two worked; it goes in the PR body and the README later.

- [ ] **Step 3: Run it for the lead model**

Run: `cd /Users/hamseabdi/Desktop/Projects/argus && uv run python scripts/sdk_smoke.py --model claude-opus-5; echo "exit=$?"`
Expected: `"ok": true`, `exit=0`. If the model id is rejected, try `--model opus` and record that the alias is required.

- [ ] **Step 4: Lint and commit**

```bash
cd /Users/hamseabdi/Desktop/Projects/argus && uv run ruff check . && uv run ruff format --check . && git add scripts/sdk_smoke.py && git commit -q -m "feat(smoke): run one real SDK query and report cost" -m "The whole project depends on the Agent SDK running headless on the subscription token and reporting cost. This script is the permanent go/no-go check for that, locally and in Actions." && git log --oneline | head -1
```

### Task 7: Smoke workflow, run in Actions, pull request, gate

**Files:**
- Create: `.github/workflows/smoke.yml`

**Interfaces:**
- Consumes: `scripts/sdk_smoke.py`, repo secret `CLAUDE_CODE_OAUTH_TOKEN`
- Produces: an on-demand workflow proving CI auth; its run URL goes in the PR body

- [ ] **Step 1: Write `.github/workflows/smoke.yml`**

```yaml
name: sdk-smoke

on:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  smoke:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        model: [claude-sonnet-5, claude-opus-5]
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v10
        with:
          enable-cache: true
      - name: Install
        run: uv sync --frozen
      - name: One real query
        run: uv run python scripts/sdk_smoke.py --model ${{ matrix.model }}
        env:
          CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

Only the subscription token is passed. No `ANTHROPIC_API_KEY` exists in the repo secrets, so a passing run is proof of the free path.

- [ ] **Step 2: Commit and push**

```bash
cd /Users/hamseabdi/Desktop/Projects/argus && git add .github/workflows/smoke.yml && git commit -q -m "ci(smoke): run the SDK smoke on demand with the subscription token" -m "Proves in Actions, not just locally, that the SDK runs headless on CLAUDE_CODE_OAUTH_TOKEN. Manual trigger only, so it never burns quota unattended." && git push -u origin feat/sdk-smoke
```

- [ ] **Step 3: Confirm the secret exists**

Run: `gh secret list -R hamseabd/argus`
Expected: `CLAUDE_CODE_OAUTH_TOKEN` listed. If not, wait for Hamse to finish Task 5 Step 0.

- [ ] **Step 4: Run the workflow on the branch and watch it**

```bash
cd /Users/hamseabdi/Desktop/Projects/argus && gh workflow run smoke.yml --ref feat/sdk-smoke && sleep 5 && gh run list --workflow smoke.yml --limit 1 --json databaseId -q '.[0].databaseId'
```
Then: `gh run watch <id> --exit-status` and `gh run view <id> --log | grep -E '"ok"|credential source'`.
Expected: both matrix jobs succeed; two log lines with `"ok": true` and `credential source: CLAUDE_CODE_OAUTH_TOKEN`.

- [ ] **Step 5: Run the full local verification and keep the output**

Run:
```bash
cd /Users/hamseabdi/Desktop/Projects/argus && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```
Expected: clean lint and format, `7 passed`.

- [ ] **Step 6: Open the PR with evidence**

````bash
cd /Users/hamseabdi/Desktop/Projects/argus && gh pr create --base main --head feat/sdk-smoke --title "feat(smoke): prove the SDK runs on the subscription token" --body "$(cat <<'EOF'
## Increment 1: SDK smoke (go/no-go gate)

Adds `argus/auth.py` (credential detection), `scripts/sdk_smoke.py` (one real query, prints cost), and the on-demand `sdk-smoke` workflow that runs it with only `CLAUDE_CODE_OAUTH_TOKEN`.

### Definition of done
- [x] Smoke passes locally
- [x] Smoke passes in Actions with only the subscription token: <run URL>
- [x] Both `claude-sonnet-5` and `claude-opus-5` accepted
- [x] CI green on this PR

### Verification (local)
```
<paste Step 5 output>
<paste the two local smoke JSON lines from Task 6>
```

### Verification (Actions)
```
<paste the two "ok" lines and the credential-source lines from Task 7 Step 4>
```
EOF
)"
````

- [ ] **Step 7: Wait for CI, then STOP for merge**

Run: `cd /Users/hamseabdi/Desktop/Projects/argus && gh pr checks --watch`
Report the PR URL, the smoke run URL, and the recorded facts (machine-login behavior, model id acceptance, cost per query) to Hamse.
Do not start increment 2 until the PR is merged.
After the merge: `git checkout main && git pull --ff-only && git branch -d feat/sdk-smoke`.

---

## What the next plan needs from this one

When increment 1 is merged, the next plan (increments 2 to 4) starts from these confirmed facts, which Task 6 and Task 7 record in the PR body:

- Whether the SDK's bundled runtime uses the machine login locally, or the token must be exported.
- Whether `claude-opus-5` and `claude-sonnet-5` are accepted as-is or the `opus` and `sonnet` aliases are required.
- The measured cost and duration of a one-turn query per model.
- The installed `claude-agent-sdk` version, so the next plan can read `ResultMessage`, `AgentDefinition`, `HookMatcher`, and `output_format` from the installed package instead of from memory.
