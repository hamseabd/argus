# Argus eval report

Run `2026-11-02-0ce8e7a`.
Lead `claude-opus-5-5` (high), specialists `claude-opus-5-5` (high), verifier `claude-opus-5-5` (high); prompts `ab0548ebe606`.

How to read it and how it is produced: see Evals in the README.

| Mode | Precision | Recall | Clean-control FP rate | Verifier accuracy | TP / FP / FN | Cost per case | Turns per completed review | Latency per completed review | Errors |
|---|---|---|---|---|---|---|---|---|---|
| verify | 50% | 95% | 17% (1/6) | 51% | 21 / 21 / 1 | $0.46 | 14.1 | 55 s | 0 |

| Category | Mode | Caught / seeded | Recall |
|---|---|---|---|
| conventions | verify | 2 / 2 | 100% |
| correctness | verify | 8 / 8 | 100% |
| design | verify | 2 / 3 | 67% |
| maintainability | verify | 3 / 3 | 100% |
| security | verify | 3 / 3 | 100% |
| tests | verify | 3 / 3 | 100% |

| Category | verify: TP / FP (precision) |
|---|---|
| correctness | 8 / 2 (80%) |
| security | 3 / 2 (60%) |
| tests | 3 / 5 (38%) |
| maintainability | 3 / 5 (38%) |
| design | 2 / 6 (25%) |
| conventions | 2 / 1 (67%) |

## Specialists

Scored on what each recorded, before the lead's triage.

| Specialist | Mode | Caught / owned (recall) | True / false findings (precision) |
|---|---|---|---|
| correctness | verify | 8 / 8 (100%) | 19 / 2 (90%) |
| security | verify | 3 / 3 (100%) | 8 / 2 (80%) |
| tests | verify | 3 / 3 (100%) | 4 / 5 (44%) |
| maintainability | verify | 3 / 3 (100%) | 18 / 10 (64%) |
| design | verify | 3 / 3 (100%) | 23 / 8 (74%) |
| conventions | verify | 2 / 2 (100%) | 3 / 0 (100%) |
| lead | verify | 0 / 0 (n/a) | 3 / 0 (100%) |

## Lead

Bugs lost: a specialist recorded the bug and the review no longer held it.

| Mode | Bugs lost | Duplicates posted | False findings kept / removed | Nit-cap breaches | Findings-cap breaches |
|---|---|---|---|---|---|
| verify | 0 | 1 | 15 / 12 | 0 | 0 |

## Verifier

Its own answer against seeded truth, with the mean confidence in each cell.

| Mode | Confirmed a real bug | Rejected a real bug | Confirmed a false finding | Rejected a false finding | Inconclusive |
|---|---|---|---|---|---|
| verify | 22 (0.93) | 0 | 21 (0.80) | 0 | 0 |

## Verifier traps

Plausible non-bugs: triggered when a specialist flagged one, posted when it survived.

| Mode | Traps | Triggered | Posted |
|---|---|---|---|
| verify | 4 | 1 | 1 |

| Case | verify: TP / FP / FN |
|---|---|
| argus_dev_pr55_c4255102517 | 1 / 7 / 0 |
| clean_added_log_context | 0 / 1 / 0 |
| clean_correct_fix | 0 / 0 / 0 |
| clean_docs_only | 0 / 0 / 0 |
| clean_named_constant | 0 / 0 / 0 |
| clean_rename_refactor | 0 / 0 / 0 |
| clean_tested_feature | 0 / 0 / 0 |
| conventions_bare_exception | 1 / 0 / 0 |
| conventions_print_not_logger | 1 / 1 / 0 |
| cross_file_contract | 1 / 0 / 0 |
| design_layering_violation | 0 / 1 / 1 |
| design_n_plus_one | 1 / 0 / 0 |
| design_unbounded_growth | 1 / 0 / 0 |
| js_async_foreach | 1 / 0 / 0 |
| leaked_file_handle | 1 / 0 / 0 |
| maintainability_docstring_drift | 1 / 2 / 0 |
| maintainability_duplicated_logic | 1 / 0 / 0 |
| maintainability_misleading_comment | 1 / 0 / 0 |
| missing_await | 1 / 0 / 0 |
| none_deref | 1 / 0 / 0 |
| off_by_one | 1 / 0 / 0 |
| path_traversal | 1 / 0 / 0 |
| removed_auth_check | 1 / 0 / 0 |
| sqli | 1 / 1 / 0 |
| tests_missing_negative_case | 1 / 0 / 0 |
| tests_mocked_unit | 1 / 1 / 0 |
| tests_tautological_assert | 1 / 1 / 0 |
| trap_except_reraise | 0 / 0 / 0 |
| trap_inclusive_range | 0 / 0 / 0 |
| trap_subprocess_list_args | 0 / 3 / 0 |
| trap_validated_path | 0 / 3 / 0 |
| truncated_diff_bug | 1 / 0 / 0 |
