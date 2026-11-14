# Security policy

## Supported versions

| Version | Supported |
| --- | --- |
| `main` | Yes |
| `v1` tag | Yes |
| Any other commit | No |

## Reporting a vulnerability

Report privately through GitHub's private vulnerability reporting on the public release repository, [hamseabd/argus](https://github.com/hamseabd/argus): [open a report](https://github.com/hamseabd/argus/security/advisories/new) from its Security tab.
Do not open a public issue, pull request, or discussion for a suspected vulnerability.

Include what you found, the commit or tag it affects, and the smallest steps that reproduce it.
A proof of concept against your own repository is welcome; do not test against repositories you do not own.

## What to expect

An acknowledgement within 7 days.
An assessment, and a fix or a plan for one, within 30 days of the acknowledgement.

## Scope

In scope:

- The reusable workflow in `.github/workflows/review.yml`: anything that lets a pull request run its own code next to the secrets, widens the workflow or GitHub App token beyond what it declares, or leaks a secret into logs, artifacts, or review comments.
- The review pipeline in `argus/`: anything that lets a reviewed repository write, execute, or reach the network through the agent, bypass the read-only tool set or the `PreToolUse` deny hook, or load its own settings, hooks, or `CLAUDE.md` into the reviewer.
- Prompt-injection resistance: content in a diff, file, or earlier review comment that makes Argus post something it did not verify, suppress a real finding, or act outside its read-only role.

Out of scope:

- A calling repository's own workflow configuration, such as the permissions, triggers, secrets, or pinned ref it chooses when it calls the reusable workflow.
- Findings Argus misses or reports wrongly with no attacker involved; open an ordinary issue for those.
- Vulnerabilities in GitHub Actions, the Claude Agent SDK, or other dependencies themselves; report those upstream.
