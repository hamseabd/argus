# Review rules

Argus reads this file from the default branch when it reviews its own pull requests; contributors read it before opening one.
The coding conventions are in `CLAUDE.md`.

## Extra checks

- A change to the prompts (`argus/agent/prompts/`) or to the pipeline (`argus/pipeline.py`, `argus/agent/`) carries the smoke eval table in the pull request body.
  The `argus-eval` workflow runs the smoke subset on every such pull request and posts the table as a comment; copy it into the body.
- A pull request that adds or removes a specialist, or changes a model or an effort, also carries a full corpus run: label it `eval-full` and put that table in the body.
- `evals/baseline.json` changes only in a pull request whose body shows the full run it came from (`uv run python -m evals.gate baseline <record>`).
- A new corpus case from a real finding (`python -m evals.propose`) is added only on the maintainer's word, with its lines and its kind checked by hand.

## The gate

The smoke check fails when the run misses more seeded bugs than the baseline did, counted case by case, beyond `max_extra_misses` in `evals/thresholds.yaml`, or when a clean control reports more findings than in the baseline.
`max_extra_misses` allows a little noise: one review per case is one sample, and a model's findings vary between runs. The values themselves live only in `evals/thresholds.yaml`.
A baseline case the run should have covered but did not fails the check too, unless the change removed it from the corpus.

## What a pull request is trusted with

On a pull request the `argus-eval` workflow takes from the pull request only what it is testing: the Argus package, the reviewer whose findings are scored.
Everything that judges it comes from the default branch: the smoke cases and their `expected.yaml`, the scorer and the gate (`evals/score.py`, `evals/gate.py`), the baseline, and the thresholds.
So a change to any of those takes effect once merged, never on the pull request that makes it. An `eval-full` run reviews the pull request's own corpus, so new cases are measured, but it is scored and gated the same way.
The gate runs in its own job, from fresh checkouts of both branches, and takes only the record from the eval job; the pull request's commit is read there only for the list of cases it keeps, so a case it removes is listed rather than failed.
The gate fails closed: a result the default branch's models cannot read is an error. A pull request that changes those models fails the check until it merges; the maintainer merges it past the red check knowingly, with a full run in the body.
What the gate cannot check: the record is written by the pull request's own code, so it trusts the run to have reviewed the cases it names. It stops a change from loosening its judges, not a dishonest change, which is what review is for.
An errored case fails the check too: it was not measured.
