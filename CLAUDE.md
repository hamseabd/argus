# CLAUDE.md - Argus

## What this is

Argus is a code-review agent built on the Claude Agent SDK (Python).
It reviews a PR or a local diff with a lead reviewer plus three parallel read-only specialist subagents, verifies each finding with a second query, and posts inline GitHub review comments.
It is a portfolio project: engineering quality and a readable architecture matter more than feature count.
The design spec and plans live locally under `docs/` and are not committed; read the spec before changing behavior.

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
