"""Evaluation harness: score predictions against labeled cases.

Metrics (as required by problem_statement.md):
- status accuracy:   deletion_status matches
- step accuracy:     precision / recall / F1 over whole steps (all 7 fields equal),
                     ignoring position
- field accuracy:    per field, share of aligned steps (same step_no) that match
- sequence accuracy: share of cases whose steps_json array matches exactly, in order

Usage:
    python3 code/evaluation/main.py            # score the 32 public samples
"""

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIELDS = ["step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"]


def step_key(s):
    return tuple(s[f] for f in FIELDS[1:])          # step_no excluded: position is scored separately


def score(predicted_rows, expected_rows):
    pred = {r["case_id"]: r for r in predicted_rows}
    status_ok = exact = tp = n_pred = n_exp = 0
    field_hits, field_total = Counter(), 0
    failures = []
    for e in expected_rows:
        p = pred.get(e["case_id"])
        e_steps = json.loads(e["steps_json"])
        p_steps = json.loads(p["steps_json"]) if p else []
        if p and p["deletion_status"] == e["deletion_status"]:
            status_ok += 1
        if p and p["deletion_status"] == e["deletion_status"] and p_steps == e_steps:
            exact += 1
        else:
            failures.append(e["case_id"])
        overlap = Counter(map(step_key, p_steps)) & Counter(map(step_key, e_steps))
        tp += sum(overlap.values())
        n_pred += len(p_steps)
        n_exp += len(e_steps)
        for ps, es in zip(p_steps, e_steps):
            for f in FIELDS:
                field_hits[f] += ps[f] == es[f]
        field_total += max(len(p_steps), len(e_steps))

    n = len(expected_rows)
    precision = tp / n_pred if n_pred else 0.0
    recall = tp / n_exp if n_exp else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "cases": n,
        "status_accuracy": status_ok / n,
        "sequence_accuracy": exact / n,
        "step_precision": precision,
        "step_recall": recall,
        "step_f1": f1,
        "field_accuracy": {f: field_hits[f] / field_total if field_total else 0.0 for f in FIELDS},
        "failures": failures,
    }


def diff(pred_row, exp_row):
    """Human-readable difference for one case."""
    p = Counter(map(step_key, json.loads(pred_row["steps_json"])))
    e = Counter(map(step_key, json.loads(exp_row["steps_json"])))
    lines = [f"{exp_row['case_id']}: predicted {pred_row['deletion_status']}, expected {exp_row['deletion_status']}"]
    for s in sorted((e - p).elements()):
        lines.append(f"   missing  {s}")
    for s in sorted((p - e).elements()):
        lines.append(f"   extra    {s}")
    return "\n".join(lines)


def evaluate_samples(use_cached_models=True):
    """Run the full pipeline on the 32 labeled samples.

    With use_cached_models, vision and intent answers come from the LLM cache only
    (LLM_CACHE_ONLY=1), so evaluation is reproducible and never spends quota; anything
    not cached falls back to the deterministic path.
    """
    import os
    from pipeline import AuditPipeline

    media_obs, intents = [], None
    if use_cached_models:
        os.environ["LLM_CACHE_ONLY"] = "1"
        from loaders import load_all
        from vision import parse_media
        from intent import classify_all
        data = load_all()
        media_obs, _ = parse_media(data["evidence_files"], log=lambda *a: None)
        decisions = classify_all(data["sample_requests"], log=lambda *a: None)
        intents = {c: d for c, (d, _) in decisions.items()}
        pipe = AuditPipeline(data, extra_observations=media_obs)
    else:
        pipe = AuditPipeline()
    rows = pipe.run(pipe.data["sample_requests"], intents)
    return pipe, rows


if __name__ == "__main__":
    pipe, rows = evaluate_samples(use_cached_models="--no-llm" not in sys.argv)
    result = score(rows, pipe.data["sample_output"])
    print(json.dumps({k: v for k, v in result.items() if k != "failures"}, indent=2))
    print(f"\nfailing cases ({len(result['failures'])}):")
    by_id = {r["case_id"]: r for r in rows}
    for e in pipe.data["sample_output"]:
        if e["case_id"] in result["failures"]:
            print(diff(by_id[e["case_id"]], e))
