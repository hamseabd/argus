# Argus - Design Spec

Date: 2026-09-10
Status: approved in brainstorming, pending written-spec review

## 1. Purpose

Argus is a code-review agent built on the Claude Agent SDK (Python).
It reviews a pull request or a local diff, verifies its own findings, and posts inline review comments on GitHub.
It exists as a portfolio project: the goal is to demonstrate that the author can build SDLC agents on the Agent SDK with production-grade engineering.
It is not intended to run as a product for other users.

Success looks like: a public repo with a clear architecture, a real PR on that repo reviewed by Argus with inline comments, green CI, and a README a hiring manager can read in five minutes.

## 2. Constraints

- Cost to operate is $0.
  Public repo for free GitHub Actions minutes, no cloud resources, and Claude auth through the author's subscription token, never a pay-per-token API key.
- Python 3.12, `pyproject.toml`, `uv` for environments and lockfile, `ruff`, `pytest`.
- Read-only agent.
  Argus never edits, writes, or executes code in the target repository.
- Structured logs, not print statements.
- No infrastructure beyond GitHub Actions.

## 3. Architecture

Python owns the pipeline stages and all orchestration.
The Agent SDK owns the fan-out inside the review stage via subagents.

```
PR number or local diff
  1. context    fetch unified diff, changed files, PR metadata
  2. review     one query(): lead reviewer delegates to 3 parallel read-only
                specialist subagents, merges, returns a Review as structured
                output (schema-validated by the SDK, re-validated by Pydantic)
  3. verify     one cheap query() per finding, bounded concurrency,
                returns a Verdict as structured output
  4. rank       drop rejected, order by severity, keep unverified with a label
  5. report     terminal markdown, JSON artifact, or GitHub review with inline comments
  telemetry     structured JSON logs: cost, tokens, turns, duration per stage,
                plus a tool-call audit trail from hooks
```

Every stage is a plain function or coroutine over domain models.
The only package that imports `claude_agent_sdk` is `argus/agent/`.

## 4. Repository layout

```
argus/
  pyproject.toml
  uv.lock
  README.md
  CLAUDE.md
  .github/workflows/ci.yml          lint + test on push and PR
  .github/workflows/review.yml      dogfood: Argus reviews PRs on this repo
  argus/
    __init__.py
    cli.py                          Typer CLI
    settings.py                     pydantic-settings
    telemetry.py                    structlog configuration and event helpers
    pipeline.py                     stage orchestration
    domain/
      models.py                     Pydantic v2 models, zero SDK imports
      errors.py                     typed exceptions
    context/
      diff.py                       unified diff parser, commentable-line index
      git.py                        local diff via git subprocess
      github.py                     httpx client: PR diff, files, metadata, post review
    agent/
      options.py                    builds ClaudeAgentOptions for lead and verifier
      schemas.py                    output_format payloads derived from the domain models
      tools.py                      in-process MCP server: git_history (read-only)
      hooks.py                      PreToolUse deny list, PostToolUse audit log
      runner.py                     runs a query, streams messages, extracts metrics
      prompts/
        lead.md
        correctness.md
        security.md
        quality.md
        verifier.md
    report/
      markdown.py                   terminal report and review body
      github_review.py              maps findings to diff lines, builds review payload
  tests/
    fixtures/
      seeded_bug_repo/              tiny package with an off-by-one and a SQL injection
      diffs/                        unified diff fixtures for the parser
    unit tests per module
    test_live.py                    opt-in, marker "live", runs the real SDK
  docs/superpowers/specs/
```

## 5. Domain models

All models are Pydantic v2 in `argus/domain/models.py`.

```
Severity  = Literal["critical", "high", "medium", "low"]
Category  = Literal["correctness", "security", "quality"]
Status    = Literal["pending", "confirmed", "rejected", "unverified"]

Finding
  id: str                      assigned by the pipeline after parsing, e.g. "security-2"
  file: str                    repo-relative path
  line: int (>= 1)             line number in the new version of the file
  end_line: int | None
  severity: Severity
  category: Category
  title: str (<= 100 chars)
  description: str             what is wrong and why it matters
  evidence: str                the code path or reasoning that supports the claim
  suggested_fix: str | None
  confidence: float (0..1)
  status: Status = "pending"

Review
  summary: str                 2 to 5 sentences
  findings: list[Finding]      max 25
  files_reviewed: list[str]

Verdict
  finding_id: str
  verdict: Literal["confirmed", "rejected"]
  reasoning: str
  confidence: float (0..1)

StageMetrics
  stage: str                   "review" or "verify:<finding_id>"
  model: str
  cost_usd: float
  input_tokens: int
  output_tokens: int
  num_turns: int
  duration_ms: int
  subagents_run: int           review stage only, counted from SubagentStart events

ReviewResult
  review: Review               findings carry their final status
  verdicts: list[Verdict]
  metrics: list[StageMetrics]
  total_cost_usd: float
  session_id: str

ChangedFile
  path: str
  status: Literal["added", "modified", "removed", "renamed"]
  previous_path: str | None

PRInfo
  owner: str, repo: str, number: int
  title: str, body: str
  base_ref: str, head_ref: str, head_sha: str
  html_url: str

ReviewContext
  source: Literal["pr", "local"]
  repo_root: Path
  diff_text: str
  files: list[ChangedFile]
  truncated_files: list[str]   files omitted from diff_text because of the size cap
  pr: PRInfo | None
```

## 6. Context stage

PR mode fetches the unified diff, the changed-file list, and PR metadata from the GitHub REST API with `httpx`, authenticated by `GITHUB_TOKEN`.
Local mode runs `git diff <base>...HEAD` (default base `main`) plus the working tree, and lists changed files from the same command.

The diff parser in `context/diff.py` produces, per file, the set of new-side line numbers that appear in a hunk as added or context lines.
That index is what decides whether a finding can be an inline comment.

Size cap: if the diff exceeds 200 KB, whole files are dropped from `diff_text` starting with the largest, until it fits.
Dropped files are listed in `truncated_files` and named in the lead prompt so the agent can read them with `Read`.

PR mode assumes the current working directory is a checkout of the PR head.
The CLI compares `git rev-parse HEAD` to the PR head SHA and logs a warning on mismatch.

## 7. Review stage

One `query()` call with a lead reviewer and three subagents.

### Lead reviewer

- Model: `claude-opus-5`, effort `high`.
- System prompt: `prompts/lead.md`.
  It states the role, the review standard (actionable defects only, no style nits), the requirement to delegate to all three specialists with the same diff context, and the output contract: the final answer is a `Review`.
- User prompt: PR title and body if present, the changed-file list, the diff, and the list of truncated files.
- Tools: `Read`, `Grep`, `Glob`, `Agent`, `mcp__argus__git_history`.
- `output_format` set to the `Review` schema (see Structured output below).
- `permission_mode="dontAsk"` so any unlisted tool is denied instead of blocking.
- `cwd` is the repository root.
- Caps: `max_turns=40`, `max_budget_usd=3.00`.

### Specialists

Defined with the SDK `agents` option so the lead can delegate with the `Agent` tool.
They run in parallel.

| Name          | Focus                                                                 | Model            | Effort |
|---------------|-----------------------------------------------------------------------|------------------|--------|
| correctness   | logic errors, edge cases, error paths, concurrency, resource leaks     | `claude-sonnet-5` | medium |
| security      | injection, secrets, authz gaps, unsafe deserialization, SSRF, path traversal | `claude-sonnet-5` | medium |
| quality       | missing or weak tests for changed behavior, dead code, API misuse     | `claude-sonnet-5` | medium |

Each specialist has `tools=["Read", "Grep", "Glob", "mcp__argus__git_history"]` and `max_turns=15`.
Each returns a JSON array of finding objects (without `id` and `status`) as its final message.
The lead merges the three lists, removes duplicates, and returns the `Review`.

### Structured output

The lead query sets `output_format={"type": "json_schema", "schema": ...}` with a schema generated from the `Review` model in `agent/schemas.py`.
The SDK validates the final answer against the schema and re-prompts the model on mismatch.
`runner.py` reads `ResultMessage.structured_output`, validates it again with `Review.model_validate`, and the pipeline assigns finding ids.
The schema excludes `id` and `status`, which are pipeline-owned.

The SDK validator speaks JSON Schema draft-07, so the models stay flat and avoid keywords outside that draft.
A unit test asserts the generated schema is acceptable.

### Custom tool: git_history

`argus/agent/tools.py` builds an in-process MCP server named `argus` with one read-only tool.
`git_history(path, start_line, end_line)` runs `git log -L` for that range and returns the recent commits that touched it: sha, date, author, subject.
It exists so reviewers and the verifier can tell a regression from an intentional change without being given `Bash`.
The tool rejects paths outside the repository root and returns an empty list for untracked files.

### Hooks

`argus/agent/hooks.py` registers two hooks on every query.

- `PreToolUse` matching `Write|Edit|MultiEdit|NotebookEdit|Bash|WebFetch|WebSearch`: deny with a reason and log a `tool_denied` event.
  This is defense in depth on top of `allowed_tools`.
- `PostToolUse` matching everything: log a `tool_call` event with the tool name and a truncated input summary.
- `PostToolUseFailure` matching everything: log a `tool_failed` event.
- `SubagentStart` and `SubagentStop`: log `subagent_start` and `subagent_stop`; the start count feeds `StageMetrics.subagents_run`.

Python hooks are registered as `hooks={"PreToolUse": [HookMatcher(matcher=..., hooks=[fn])], ...}`.
A deny is returned as `hookSpecificOutput.permissionDecision = "deny"` with a `permissionDecisionReason` so the model does not retry.
If fewer than three subagents ran, the pipeline logs a warning; it does not fail the run.

## 8. Verify stage

For each finding in the review, run one fresh `query()`:

- Model `claude-sonnet-5`, effort `medium`.
- System prompt `prompts/verifier.md`: given one finding and its diff hunk, try to refute it by reading the code; confirm only if the code path actually exhibits the issue.
- Tools: `Read`, `Grep`, `Glob`, `mcp__argus__git_history`.
- `output_format` set to the `Verdict` schema without `finding_id`, which the pipeline supplies.
- Caps: `max_turns=10`, `max_budget_usd=0.50`.

Verifications run with `asyncio.gather` under a `Semaphore(4)`.
A rejected finding gets `status="rejected"` and is dropped from the report.
A confirmed finding gets `status="confirmed"`.
If a verify query fails or ends without a verdict, the finding gets `status="unverified"` and stays in the report with that label.
`--no-verify` skips the stage and every finding is reported as `unverified`.

## 9. Rank and report

Ranking: confirmed before unverified, then severity order critical, high, medium, low, then file path.

Terminal report (`report/markdown.py`): summary, one block per finding with severity, title, location, description, evidence, suggested fix, and a footer with cost, tokens, turns, duration, and subagents run.

JSON artifact: `ReviewResult.model_dump_json()` written to the `--json` path.

GitHub review (`report/github_review.py`):

- A finding is inline when its `file` is a changed file and its `line` is in that file's commentable-line index.
  Otherwise it is listed in the review body.
- Inline comment body: severity label, title, description, evidence, and suggested fix as a fenced code block.
  GitHub suggestion syntax is not used in v1 because fixes are freeform.
- Review `event` is always `COMMENT`.
  Argus never requests changes; merge gating is done by the CLI exit code.
- The body starts with the marker `<!-- argus:review -->` and ends with the metrics footer.
- Posted with `POST /repos/{owner}/{repo}/pulls/{number}/reviews`, `commit_id` set to the PR head SHA.

## 10. CLI

```
argus review --pr <n> [--repo owner/name] [--post] [--json <path>] [--no-verify] [--fail-on <severity>]
argus review --diff [--base <ref>] [--json <path>] [--no-verify] [--fail-on <severity>]
argus version
```

- `--repo` defaults to `GITHUB_REPOSITORY`, then the `origin` remote.
- `--post` requires `--pr` and `GITHUB_TOKEN`.
- `--fail-on <severity>` exits 2 if any confirmed or unverified finding is at or above that severity.
- Model, effort, caps, and verify concurrency are settings, overridable by `ARGUS_*` environment variables.

Exit codes: 0 success, 1 error, 2 severity gate tripped.

## 11. Authentication

Accepted credentials, checked at startup, first match wins:

1. `CLAUDE_CODE_OAUTH_TOKEN` - subscription token from `claude setup-token`.
   This is the CI path and the reason the project costs nothing.
2. `ANTHROPIC_API_KEY` - supported for completeness, not used by the author.
3. Locally with neither set, the SDK's bundled runtime uses the machine's Claude Code login.

If none are available the CLI exits 1 and names all three options.

Risk: the SDK honoring the subscription token in a headless CI run is not confirmed by the docs.
The first implementation task is a spike that runs one real query on the token and prints `total_cost_usd`.
If the spike fails, work stops and the constraint is revisited with the author.

## 12. GitHub Action

`.github/workflows/review.yml`:

- Triggers: `pull_request` with types `opened` and `ready_for_review`, and `workflow_dispatch` with a `pr` input.
  `synchronize` is intentionally off so each push does not burn subscription quota or post duplicate reviews.
- Skips draft PRs.
- `permissions: contents: read, pull-requests: write`.
- `concurrency: argus-${{ github.event.pull_request.number }}` with `cancel-in-progress: true`.
- Steps: checkout, `astral-sh/setup-uv`, `uv sync --frozen`, `uv run argus review --pr N --post --json argus-review.json`, upload the JSON as an artifact.
- Env: `CLAUDE_CODE_OAUTH_TOKEN` from repo secrets, `GITHUB_TOKEN` from the workflow token.

`.github/workflows/ci.yml`: `ruff check`, `ruff format --check`, `pytest -q` on pushes to `main` and on every PR, with uv caching and a concurrency group.

## 13. Telemetry

`structlog` configured in `telemetry.py`.
JSON lines to stderr when not attached to a TTY or when `ARGUS_LOG_FORMAT=json`; console renderer on a TTY.

Events:

- `run_start`, `run_end` with source, repo, PR number, total cost, finding counts by status
- `stage_start`, `stage_end` with every `StageMetrics` field
- `tool_call`, `tool_denied`, `tool_failed` from hooks
- `subagent_start`, `subagent_stop`
- `rate_limited` when the SDK reports a rate-limit message
- `review_posted` with the review URL

Every event carries `run_id` and, once known, `session_id`.

## 14. Error handling

- A `ResultMessage` with a non-success subtype, including `error_max_structured_output_retries`, raises `AgentRunError(subtype, cost_usd, session_id)`.
  The CLI prints the subtype and cost, exits 1.
- `ReviewProtocolError` when a query ends with subtype `success` but no `structured_output`.
  The SDK docs say to treat that case as a failure.
- Verify failures are per-finding and never abort the run; the finding becomes `unverified`.
- `GitHubError` wraps non-2xx responses with status and body.
  A posting failure after a completed review still writes the JSON artifact before exiting 1.
- Missing credentials, missing `GITHUB_TOKEN` with `--post`, and a `--pr` without a resolvable repo all fail before any query runs.

## 15. Testing

Unit tests run offline in CI:

- `domain/models.py`: field constraints, status transitions, ranking order.
- `agent/schemas.py`: generated schemas use only draft-07 keywords, and a sample `Review` round-trips through them.
- `agent/tools.py`: `git_history` against a temporary git repo: commits touching a line range, a path outside the root is rejected, an untracked file returns an empty list.
- `agent/hooks.py`: deny matrix for every tool name; allow for Read, Grep, Glob, Agent, MCP tools.
- `context/diff.py`: hunk parsing, commentable-line index, renamed and removed files, the size cap.
- `context/github.py`: `respx`-mocked responses for diff, files, metadata, post review, and error paths.
- `report/github_review.py`: inline versus body placement, payload shape.
- `report/markdown.py`: rendering snapshot.
- `pipeline.py`: full flow against a `FakeRunner` that returns canned `Review` and `Verdict` objects, including the unverified path and `--no-verify`.
- `cli.py`: argument validation and exit codes with the pipeline stubbed.

Live test, opt-in with `pytest -m live`:

- Runs `argus review --diff` on `tests/fixtures/seeded_bug_repo/` with the real SDK and asserts at least one confirmed finding at the seeded file.

## 16. Definition of done for v1

1. `argus review --diff` on the seeded-bug fixture yields a confirmed finding at the right file and line, and the terminal output is shown.
2. The Action reviews a real PR on the argus repo using only the subscription token, and the inline comments are linked from the README.
3. `ruff check`, `ruff format --check`, and `pytest` pass in CI, with output shown.
4. README contains the architecture diagram, the feature list, a sample review, and the cost per review.

## 17. Out of scope for v1

Each of these is a separate slice with its own spec.

- Eval corpus of seeded bugs with precision and recall reporting.
- Session resume so a re-review on `synchronize` only looks at new commits.
- A fix mode that proposes patches.
- Running the target repository's test suite inside a sandbox.
- A webhook service or any cloud deployment.
- Dismissing or updating earlier Argus reviews on re-run.

## 18. Risks

| Risk | Mitigation |
|------|------------|
| SDK does not honor the subscription token headless | Spike first; stop and decide if it fails |
| Lead skips a specialist | Prompt makes delegation mandatory; `subagents_run` is logged and a warning is emitted |
| Inline comment on a non-diff line is rejected by GitHub | Commentable-line index built from the parsed diff; unit tests on fixtures |
| Subscription rate limits mid-review | `rate_limited` event logged; caps keep runs short |
| Model id strings rejected by the SDK | Spike verifies `claude-opus-5` and `claude-sonnet-5`; aliases `opus` and `sonnet` are the fallback |
| Generated JSON schema uses a keyword the SDK validator rejects | Models stay flat; a `schemas.py` unit test checks draft-07 compatibility |

## 19. Delivery process

Work ships in increments.
Each increment is one branch, one pull request, one squash-merge onto `main`, and nothing starts on the next increment until the previous one is merged.

Branching and PRs:

- `main` is protected: pull request required, CI status check required, no direct pushes, no force pushes.
  No required reviewer, since the author is solo; the author merges after verification.
- Branch names follow the commit type: `chore/scaffold`, `feat/domain-models`, `ci/review-action`.
- Commit subjects follow `type(scope): imperative summary`; bodies explain why.
- PR title is the squash commit subject.
  PR body states the increment's definition of done and pastes the verification evidence: lint, format, and test output, plus the increment-specific check.
- Once the review Action exists, every later Argus PR is reviewed by Argus itself.
  That dogfooding is part of the showcase.

Per-increment verification, always shown in the PR body and in chat:

1. `uv run ruff check .` and `uv run ruff format --check .` clean.
2. `uv run pytest -q` green, with the count.
3. CI green on the PR.
4. The increment's own acceptance check below.

Implementation follows test-driven development: a failing test first, the minimum code to pass, then refactor.

Increments and their acceptance checks:

| # | Branch | Delivers | Acceptance check |
|---|--------|----------|------------------|
| 0 | `chore/scaffold` | GitHub repo (public), `pyproject.toml`, `uv.lock`, ruff and pytest config, `ci.yml`, `.gitignore`, MIT license, README stub, `CLAUDE.md`, branch protection | CI green on the PR; `argus version` prints |
| 1 | `feat/sdk-smoke` | `scripts/sdk_smoke.py` runs one real `query()` and prints model, cost, turns; `smoke.yml` runs it on `workflow_dispatch` with the subscription secret | Smoke passes locally and in Actions with only `CLAUDE_CODE_OAUTH_TOKEN`; the go/no-go for the whole project |
| 2 | `feat/domain-and-diff` | `domain/models.py`, `domain/errors.py`, `context/diff.py`, `context/git.py`, fixtures | Unit tests for models, parser, commentable-line index, size cap, local diff |
| 3 | `feat/agent-layer` | `agent/schemas.py`, `agent/options.py`, `agent/hooks.py`, `agent/tools.py`, `agent/runner.py`, prompts, `telemetry.py`, `settings.py` | Unit tests for schemas, hooks, `git_history`, runner against a fake message stream |
| 4 | `feat/pipeline-and-cli` | `pipeline.py`, `report/markdown.py`, `cli.py` with `--diff`, seeded-bug fixture, live test | `pytest` green with `FakeRunner`; `argus review --diff` on the fixture yields a confirmed finding at the seeded line, output shown (DoD 1) |
| 5 | `feat/github-integration` | `context/github.py`, `report/github_review.py`, `--pr` and `--post` | `respx` tests green; `argus review --pr N --post` posts a review on a test PR in this repo |
| 6 | `ci/review-action` | `review.yml` dogfood workflow | Argus reviews a real PR on this repo with only the subscription token; inline comments visible (DoD 2) |
| 7 | `docs/readme` | README with architecture diagram, features, sample review link, cost per review | Reviewed by Argus via the Action; README links to that review (DoD 4) |

DoD 3 (lint, format, tests green in CI) is checked on every increment.
