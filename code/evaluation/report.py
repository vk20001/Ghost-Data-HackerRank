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
    "Hand-written samples case_01, case_02, case_04 and case_31 prove account absence with a whole-table "
    "snapshot that simply lacks the row; the engine cites evidence only for records it actually observed.",
]


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
