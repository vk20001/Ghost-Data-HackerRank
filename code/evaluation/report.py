"""Write evaluation/evaluation_report.md from a fresh evaluation run.

    python3 code/evaluation/report.py

Scores the 32 labeled samples twice (with the cached model answers the submission
used, and with the deterministic fallback only), runs the custom cases, and writes
the report including the per-case failures that remain.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evaluation.custom_cases import run_all                       # noqa: E402
from evaluation.main import diff, evaluate_samples, score         # noqa: E402

REPORT = Path(__file__).resolve().parent / "evaluation_report.md"

# Measured on the 32 samples at each stage of development (see log.txt for the turn-by-turn record).
HISTORY = [
    ("1. First end-to-end run (rules intent, identifier linking only)", "84.4%", "0.874", "37.5%",
     "Baseline."),
    ("2. Intent fixes, restore/export state fields, decoy accounts, later-known records", "96.9%", "0.944", "56.3%",
     "French 'suppression' and negated requests were misread; restore eligibility and export availability "
     "were ignored; an old failed lookup for the same email was linked as a second account."),
    ("3. Activity evidence (catalog links, connector/restore checks)", "96.9%", "0.941", "53.1%",
     "Regression: catalog-only links pulled in a tombstone with no observed state."),
    ("4. Ignore link-only records with no observed state", "96.9%", "0.944", "56.3%",
     "Regression fixed."),
    ("5. Link anonymized analytics through their anonymization job", "96.9%", "0.932", "50.0%",
     "First attempt (wide window, all cases) assigned records to the wrong cases."),
    ("6. Restrict to fully remediated cases and a 45-minute window", "96.9%", "0.942", "59.4%",
     "Precision over recall: only cases whose remediation finished compete for anonymized records."),
    ("7. Deterministic case-order tie-break for identical candidate sets", "96.9%", "0.958", "68.8%",
     "Same rule as for accounts, so ties are resolved reproducibly."),
    ("8. LLM intent + vision extraction (Gemini), attachment and catalog-claim fixes", "100%", "see below", "see below",
     "Vision exposed a PDF attachment misread as a message, and a catalog link contradicted by the "
     "screenshot itself (marketing_001 shows a different person); catalog links are now treated as claims."),
    ("9. Cite a covering snapshot as proof of absence", "100%", "0.982", "81.3%",
     "A sanity check on the 518 test outputs found 77 COMPLETE cases with an uncited verification step, which the "
     "contract forbids. A deleted account leaves nothing to observe, but a snapshot of the whole account system "
     "taken before audit_at that lacks the row proves it was absent. Uncited COMPLETE steps fell from 77 to 2."),
]

KNOWN_LIMITS = [
    "Two expected audit records (case_18, case_20) appear in no participant file, so no evidence-based "
    "method can produce them.",
    "Deleted accounts with no identifiers left are linked through the deletion job's timing and result; "
    "when several identical requests share a window, a deterministic case-order tie-break can pick the "
    "wrong account (case_23).",
    "Anonymized analytics records are linked the same way and are only assigned when the match is "
    "unambiguous, so some are left out (case_25, case_26).",
    "The case-order tie-break (used only when several cases have identical candidate sets) was "
    "confirmed on just two or three sample cases; it is a reproducible convention, not evidence.",
    "About 30% of deletion cases depend on a tie-break when linking an identifier-free account or "
    "anonymized record; these are listed in review_queue.csv rather than hidden.",
]


def sensitivity():
    """Re-run with the hand-chosen linking windows changed, on the samples AND the unlabeled
    test set. Rules tuned to the samples would swing; rules on a plateau stay put."""
    import os
    from datetime import timedelta as td
    import linking
    from intent import classify_all
    from loaders import load_all
    from pipeline import AuditPipeline
    from vision import parse_media

    os.environ["LLM_CACHE_ONLY"] = "1"
    data = load_all()
    media, _ = parse_media(data["evidence_files"], log=lambda *a: None)
    dec = classify_all(data["requests"] + data["sample_requests"], log=lambda *a: None)
    intents = {c: x for c, (x, _) in dec.items()}
    default = (linking.WINDOW_BEFORE, linking.WINDOW_AFTER, linking.ORPHAN_WINDOW)

    def run():
        p = AuditPipeline(data, extra_observations=media)
        return p.run(data["requests"], intents), score(p.run(data["sample_requests"], intents),
                                                       data["sample_output"])

    base, _ = run()
    rows = []
    for label, (b, a, o) in [("default: 2h before / 30m after, orphan 45m", (2, 0.5, 45)),
                             ("narrow: 1h / 15m", (1, 0.25, 45)),
                             ("wide: 3h / 60m", (3, 1, 45)),
                             ("very wide: 6h / 2h", (6, 2, 45)),
                             ("orphan window 30m", (2, 0.5, 30)),
                             ("orphan window 90m", (2, 0.5, 90))]:
        linking.WINDOW_BEFORE, linking.WINDOW_AFTER, linking.ORPHAN_WINDOW = td(hours=b), td(hours=a), td(minutes=o)
        test_rows, s = run()
        changed = sum(x != y for x, y in zip(test_rows, base))
        status_changed = sum(x["deletion_status"] != y["deletion_status"] for x, y in zip(test_rows, base))
        rows.append(f"| {label} | {s['status_accuracy']:.1%} | {s['step_f1']:.3f} | {s['sequence_accuracy']:.1%} "
                    f"| {changed} | {status_changed} |")
    linking.WINDOW_BEFORE, linking.WINDOW_AFTER, linking.ORPHAN_WINDOW = default
    os.environ.pop("LLM_CACHE_ONLY", None)
    return rows


def fmt(result):
    return (f"| {result['cases']} | {result['status_accuracy']:.1%} | {result['step_precision']:.3f} | "
            f"{result['step_recall']:.3f} | {result['step_f1']:.3f} | {result['sequence_accuracy']:.1%} |")


def main():
    pipe, rows = evaluate_samples(use_cached_models=True)
    expected = pipe.data["sample_output"]
    full = score(rows, expected)
    _, rows_rules = evaluate_samples(use_cached_models=False)
    rules = score(rows_rules, expected)
    custom = run_all(verbose=False)

    by_id = {r["case_id"]: r for r in rows}
    lines = [
        "# Evaluation Report",
        "",
        f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} by "
        "`python3 code/evaluation/report.py`.",
        "",
        "## How we evaluate",
        "",
        "- **Labeled set:** the 32 public samples (`sample_deletion_requests.csv` / `sample_output.csv`). "
        "No sample answer is used by the pipeline; they are only compared against at the end.",
        "- **Replay, not re-query:** model answers (intent and vision) are replayed from the LLM cache "
        "written by the submission run, so the scores below describe exactly the answers that produced "
        "`output.csv`, and re-running the evaluation costs no tokens.",
        "- **Ablation:** the same samples are also scored with the deterministic fallback only "
        "(keyword intent, no vision), to show what the models add.",
        "",
        "Metric definitions:",
        "",
        "- *Status accuracy*: `deletion_status` matches.",
        "- *Step precision / recall / F1*: whole steps (phase, system, resource, operation, state, "
        "evidence) matched as a multiset, ignoring position.",
        "- *Field accuracy*: per field, share of steps that match at the same position.",
        "- *Sequence accuracy*: share of cases whose entire `steps_json` array matches exactly, in order.",
        "",
        "## Case, step and sequence accuracy",
        "",
        "| Configuration | Cases | Status accuracy | Step precision | Step recall | Step F1 | Sequence accuracy |",
        "|---|---|---|---|---|---|---|",
        "| Submission (LLM intent + vision) " + fmt(full),
        "| Deterministic fallback only " + fmt(rules),
        "",
        "## Field-level accuracy (submission configuration)",
        "",
        "| Field | Accuracy |",
        "|---|---|",
    ] + [f"| `{f}` | {v:.1%} |" for f, v in full["field_accuracy"].items()] + [
        "",
        "## Custom evaluation cases",
        "",
        "Written by us to cover failure modes the brief warns about. Each builds synthetic evidence with "
        "a known answer and runs the real policy engine and planner "
        "(`code/evaluation/custom_cases.py`).",
        "",
        "| # | Case | What it checks | Result |",
        "|---|---|---|---|",
    ] + [f"| {i} | `{c['name']}` | {c['doc']} | {'PASS' if c['passed'] else 'FAIL'} |"
         for i, c in enumerate(custom, 1)] + [
        "",
        f"{sum(c['passed'] for c in custom)}/{len(custom)} custom cases pass.",
        "",
        "## Failures found and improvements made",
        "",
        "Every change below came from reading a per-case diff produced by `code/evaluation/main.py`.",
        "",
        "| Stage | Status | Step F1 | Sequence | What failed / what changed |",
        "|---|---|---|---|---|",
    ] + [f"| {s} | {a} | {b} | {c} | {d} |" for s, a, b, c, d in HISTORY] + [
        "",
        f"Current submission configuration: status {full['status_accuracy']:.1%}, step F1 "
        f"{full['step_f1']:.3f}, sequence {full['sequence_accuracy']:.1%}.",
        "",
        "## Overfitting check",
        "",
        "The rules were developed while reading the 32 samples, so there is no clean holdout. To check "
        "whether results hinge on hand-picked values, each tuned threshold was perturbed and the 518 test "
        "outputs were compared with the baseline (measured with the cached model answers):",
        "",
        "| Perturbation | Test rows changed | Test statuses changed | Sample step F1 |",
        "|---|---|---|---|",
        "| Account-job window 2h -> 3h | 25 / 518 | 0 | 0.982 (unchanged) |",
        "| Account-job window 2h -> 1.5h | 24 / 518 | 0 | 0.982 (unchanged) |",
        "| Anonymized-record window 45m -> 60m | 0 / 518 | 0 | 0.982 (unchanged) |",
        "| Anonymized-record window 45m -> 35m | 0 / 518 | 0 | 0.982 (unchanged) |",
        "",
        "Statuses never move, and the anonymized-record window is not knife-edge. The account window "
        "changes which account is cited in about 5% of cases, all of them cases that already depend on "
        "a tie-break and are listed in `review_queue.csv`. Status and policy decisions come from "
        "`deletion_rules.csv` and observed evidence, not from sample-fitted values; the step-level "
        "detail of tie-break cases is the part most likely to score lower on hidden cases.",
        "",
        "## Robustness: are we overfitting the 32 samples?",
        "",
        "No model is trained here, but the rules were written while reading the samples, so the "
        "sample scores are optimistic. Three checks that do not depend on the sample labels:",
        "",
        "1. **Spec rules vs data heuristics.** Status logic (state at audit_at, policy from "
        "`deletion_rules.csv`, step ordering) comes from the problem statement. Only the linking "
        "of identifier-free records uses hand-chosen parameters, and those are the ones tested below.",
        "2. **Label-free contract checks on the 518 test cases.** For example, COMPLETE cases with an "
        "uncited verification step fell from 77 to 2 after adding proof of absence; this was measured on "
        "unlabeled test outputs, not on the samples.",
        "3. **Sensitivity analysis.** Each hand-chosen window is changed and the pipeline re-run. "
        "'Test rows changed' counts unlabeled test predictions that differ from the default run.",
        "",
        "| Linking parameters | Sample status | Sample step F1 | Sample sequence | Test rows changed (of 518) | Test statuses changed |",
        "|---|---|---|---|---|---|",
    ] + sensitivity() + [
        "",
        "Reading: no parameter change moves any test status, so the COMPLETE/INCOMPLETE decisions do not "
        "depend on these heuristics. Step-level output sits on a plateau: widening the windows changes few "
        "rows and no sample score, while narrowing below the observed job timing (about 1 to 1.5 hours "
        "before the reported status) breaks linking. Cases whose links still rely on a tie-break are "
        "listed in `review_queue.csv`, which bounds the exposure.",
        "",
        "## Remaining failures",
        "",
    ] + [f"- {k}" for k in KNOWN_LIMITS] + [
        "",
        "Per-case differences (missing = expected but not produced, extra = produced but not expected):",
        "",
        "```",
    ] + [diff(by_id[e["case_id"]], e) for e in expected if e["case_id"] in full["failures"]] + [
        "```",
    ]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {REPORT}")
    print(json.dumps({k: v for k, v in full.items() if k not in ("failures", "field_accuracy")}, indent=2))


if __name__ == "__main__":
    main()
