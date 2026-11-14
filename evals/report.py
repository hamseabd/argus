"""The eval tables in Markdown: a summary per mode, then one table per agent, then per case.

This is what evals/report.md holds after a full run and what the pull request
comment shows after a smoke run. Rendering only: the numbers come from
evals.score.
"""

from collections.abc import Mapping

from evals.score import CATEGORY_ORDER, VERIFIER_CELLS, CaseScore, Summary, summarize


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _num(value: float | None, fmt: str) -> str:
    return "n/a" if value is None else format(value, fmt)


def render_markdown(scores_by_mode: Mapping[str, list[CaseScore]]) -> str:
    """The summary, recall per category, each agent's table, and a row per case."""
    summaries = {mode: summarize(scores) for mode, scores in scores_by_mode.items()}
    lines = [
        *_summary_table(summaries),
        *_category_recall(summaries),
        *_trial_spread(scores_by_mode),
        *_category_precision(summaries),
        *_specialist_table(summaries),
        *_lead_table(summaries),
        *_verifier_table(summaries),
        *_trap_table(summaries),
        *_case_table(scores_by_mode),
    ]
    return "\n".join(lines) + "\n"


def _summary_table(summaries: Mapping[str, Summary]) -> list[str]:
    lines = [
        "| Mode | Precision | Recall | Clean-control FP rate | Verifier accuracy | TP / FP / FN "
        "| Cost per case | Turns per completed review | Latency per completed review | Errors |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for mode, s in summaries.items():
        latency = None if s.mean_duration_ms is None else s.mean_duration_ms / 1000
        lines.append(
            f"| {mode} | {pct(s.precision)} | {pct(s.recall)} "
            f"| {pct(s.clean_control_fp_rate)} ({s.clean_controls_flagged}/{s.clean_controls}) "
            f"| {pct(s.verifier_accuracy)}{_inconclusive(s.verifier_inconclusive)} "
            f"| {s.true_positives} / {s.false_positives} / {s.false_negatives} "
            f"| ${_num(s.mean_cost_usd, '.2f')} | {_num(s.mean_turns, '.1f')} "
            f"| {_num(latency, '.0f')} s | {s.errors} |"
        )
    return lines


def _category_recall(summaries: Mapping[str, Summary]) -> list[str]:
    lines = ["", "| Category | Mode | Caught / seeded | Recall |", "|---|---|---|---|"]
    for mode, s in summaries.items():
        for category, seeded in s.seeded_by_category.items():
            caught = s.caught_by_category.get(category, 0)
            recall = s.recall_by_category[category]
            lines.append(f"| {category} | {mode} | {caught} / {seeded} | {pct(recall)} |")
    return lines


def _trial_spread(scores_by_mode: Mapping[str, list[CaseScore]]) -> list[str]:
    lines: list[str] = []
    for mode, scores in scores_by_mode.items():
        trials = sorted({s.trial for s in scores})
        if len(trials) > 1:
            per_trial = [summarize(s for s in scores if s.trial == t) for t in trials]
            lines += [
                "",
                f"{mode} recall per trial: " + ", ".join(pct(t.recall) for t in per_trial),
                f"{mode} precision per trial: " + ", ".join(pct(t.precision) for t in per_trial),
            ]
    return lines


def _category_precision(summaries: Mapping[str, Summary]) -> list[str]:
    modes = list(summaries)
    categories = [c for c in CATEGORY_ORDER if any(c in s.by_category for s in summaries.values())]
    if not categories:
        return []
    lines = [
        "",
        "| Category | " + " | ".join(f"{m}: TP / FP (precision)" for m in modes) + " |",
        "|---|" + "---|" * len(modes),
    ]
    for category in categories:
        cells = []
        for mode in modes:
            row = summaries[mode].by_category.get(category)
            if row is None:
                cells.append("")
            else:
                precision = row["precision"]
                cells.append(
                    f"{row['true_positives']} / {row['false_positives']} "
                    f"({pct(None if precision is None else float(precision))})"
                )
        lines.append(f"| {category} | " + " | ".join(cells) + " |")
    return lines


def _specialist_table(summaries: Mapping[str, Summary]) -> list[str]:
    """Each specialist on its raw output, before the lead triaged it."""
    lines = [
        "",
        "## Specialists",
        "",
        "Scored on what each recorded, before the lead's triage.",
        "",
        "| Specialist | Mode | Caught / owned (recall) | True / false findings (precision) |",
        "|---|---|---|---|",
    ]
    for mode, s in summaries.items():
        for name, sp in s.specialists.items():
            lines.append(
                f"| {name} | {mode} | {sp.caught} / {sp.seeded} ({pct(sp.recall)}) "
                f"| {sp.true_findings} / {sp.false_findings} ({pct(sp.precision)}) |"
            )
    return lines


def _lead_table(summaries: Mapping[str, Summary]) -> list[str]:
    lines = [
        "",
        "## Lead",
        "",
        "Bugs lost: a specialist recorded the bug and the review no longer held it.",
        "",
        "| Mode | Bugs lost | Duplicates posted | False findings kept / removed "
        "| Nit-cap breaches | Findings-cap breaches |",
        "|---|---|---|---|---|---|",
    ]
    for mode, s in summaries.items():
        lead = s.lead
        lines.append(
            f"| {mode} | {lead.lost} | {lead.duplicates} "
            f"| {lead.kept_false} / {lead.removed_false} "
            f"| {lead.nit_cap_breaches} | {lead.max_findings_breaches} |"
        )
    return lines


def _verifier_table(summaries: Mapping[str, Summary]) -> list[str]:
    lines = [
        "",
        "## Verifier",
        "",
        "Its own answer against seeded truth, with the mean confidence in each cell.",
        "",
        "| Mode | Confirmed a real bug | Rejected a real bug | Confirmed a false finding "
        "| Rejected a false finding | Inconclusive |",
        "|---|---|---|---|---|---|",
    ]
    for mode, s in summaries.items():
        cells = [s.verifier_cells[c] for c in VERIFIER_CELLS]
        rendered = " | ".join(
            f"{c.count} ({_num(c.mean_confidence, '.2f')})" if c.count else "0" for c in cells
        )
        lines.append(f"| {mode} | {rendered} | {s.verifier_inconclusive} |")
    return lines


def _trap_table(summaries: Mapping[str, Summary]) -> list[str]:
    if not any(s.traps for s in summaries.values()):
        return []
    lines = [
        "",
        "## Verifier traps",
        "",
        "Plausible non-bugs: triggered when a specialist flagged one, posted when it survived.",
        "",
        "| Mode | Traps | Triggered | Posted |",
        "|---|---|---|---|",
    ]
    for mode, s in summaries.items():
        lines.append(f"| {mode} | {s.traps} | {s.traps_triggered} | {s.traps_posted} |")
    return lines


def _case_table(scores_by_mode: Mapping[str, list[CaseScore]]) -> list[str]:
    modes = list(scores_by_mode)
    by_case: dict[str, dict[str, list[CaseScore]]] = {}
    for mode, scores in scores_by_mode.items():
        for score in scores:
            by_case.setdefault(score.case, {}).setdefault(mode, []).append(score)
    lines = [
        "",
        "| Case | " + " | ".join(f"{m}: TP / FP / FN" for m in modes) + " |",
        "|---|" + "---|" * len(modes),
    ]
    for case, per_mode in sorted(by_case.items()):
        lines.append(
            f"| {case} | " + " | ".join(_case_cell(per_mode.get(m, [])) for m in modes) + " |"
        )
    return lines


def _inconclusive(count: int) -> str:
    return f" ({count} inconclusive)" if count else ""


def _case_cell(runs: list[CaseScore]) -> str:
    """A case's counts in one mode, summed over its trials; failed runs are counted, not hidden."""
    if not runs:
        return ""
    errors = sum(r.error is not None for r in runs)
    if errors == len(runs):
        return "error" if errors == 1 else f"error ({errors} runs)"
    tp = sum(r.true_positives for r in runs)
    fp = sum(r.false_positives for r in runs)
    fn = sum(r.false_negatives for r in runs)
    cell = f"{tp} / {fp} / {fn}"
    return cell + (f" ({errors} errored)" if errors else "")
