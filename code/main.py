"""Ghost Data Still Exists: deletion-audit agent. Entry point.

Run from the repository root:

    python3 code/main.py               # full run: LLM intent + vision + audit -> output.csv
    python3 code/main.py --no-llm      # deterministic only (rules for intent, no vision)

Writes output.csv at the repository root and refreshes code/evaluation/usage_report.md.
"""

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from intent import classify_all                      # noqa: E402
from loaders import REPO_ROOT, load_all              # noqa: E402
from pipeline import AuditPipeline                   # noqa: E402

OUTPUT = REPO_ROOT / "output.csv"


def validate(rows, requests):
    """Check the submission contract before writing anything."""
    expected_ids = {r["case_id"] for r in requests}
    assert len(rows) == len(requests) == len(expected_ids), "one row per request"
    assert {r["case_id"] for r in rows} == expected_ids, "case ids must match the input"
    for r in rows:
        assert r["deletion_status"] in {"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"}
        steps = json.loads(r["steps_json"])
        assert [s["step_no"] for s in steps] == list(range(1, len(steps) + 1)), r["case_id"]
        if r["deletion_status"] == "NOT_APPLICABLE":
            assert steps == [], r["case_id"]
        if r["deletion_status"] == "COMPLETE":
            assert all(s["phase"] == "VERIFICATION" for s in steps), r["case_id"]
        for s in steps:
            if s["phase"] == "REMEDIATION" or (r["deletion_status"] == "INCOMPLETE" and s["phase"] == "VERIFICATION"):
                assert s["evidence_id"] == "", r["case_id"]


def write_audit_trail(pipe, requests, intents):
    """Explainability outputs, kept next to output.csv:

    - audit_trace.jsonl: per case, which records were linked and how, the state each was
      in at audit_at, the policy requirement, and the evidence behind it.
    - review_queue.csv: cases whose decision rests on a weaker link (a tie-break between
      look-alike candidates, a missing account, or a COMPLETE verdict that includes a record
      with no evidence before audit_at). In production
      these go to a human before any destructive remediation runs.
    """
    review = []
    with open(REPO_ROOT / "audit_trace.jsonl", "w", encoding="utf-8") as f:
        for r in sorted(requests, key=lambda r: int(r["case_id"].split("_")[1])):
            if not intents.get(r["case_id"], True):
                f.write(json.dumps({"case_id": r["case_id"], "status": "NOT_APPLICABLE",
                                    "reason": "request does not authorize deletion"}) + "\n")
                continue
            _, _, trace = pipe.audit(r, True)
            f.write(json.dumps(trace, ensure_ascii=False) + "\n")
            reasons = [m for m in (trace["account_link"], trace["anonymized_record_link"]) if m and "review" in m]
            if trace["status"] == "COMPLETE":     # COMPLETE must be proven; flag anything unproven
                reasons += [f"{x['resource_id']} has no evidence before audit_at"
                            for x in trace["records"] if not x["observed_before_audit"]]
            if not trace["account"]:
                reasons.append("no account record could be linked")
            if reasons:
                review.append({"case_id": r["case_id"], "status": trace["status"], "reasons": "; ".join(reasons)})
    with open(REPO_ROOT / "review_queue.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["case_id", "status", "reasons"])
        w.writeheader()
        w.writerows(review)
    print(f"Wrote audit_trace.jsonl and review_queue.csv ({len(review)} cases flagged for human review)")


def main(use_llm=True):
    data = load_all()
    print(f"Loaded {len(data['requests'])} requests, {len(data['evidence_files'])} evidence files.")

    media_obs = []
    if use_llm:
        from vision import parse_media
        print("Reading screenshots and PDFs with the vision model...")
        media_obs, failed = parse_media(data["evidence_files"])
        print(f"  {len(media_obs)} records extracted, {len(failed)} files skipped")

    print("Building identity index, evidence table and links...")
    pipe = AuditPipeline(data, extra_observations=media_obs)
    print(f"  {len(pipe.observations)} observations")

    print("Classifying request intent...")
    # The 32 labeled samples are classified in the same run so the evaluation harness
    # can score the exact model answers that the submission used.
    decisions = classify_all(data["requests"] + data["sample_requests"], use_llm=use_llm)
    sources = {}
    for cid, (_, src) in decisions.items():
        if cid not in {r["case_id"] for r in data["requests"]}:
            continue
        sources[src] = sources.get(src, 0) + 1
    print(f"  decided by: {sources}")
    intents = {cid: is_del for cid, (is_del, _) in decisions.items()}

    rows = pipe.run(data["requests"], intents)
    write_audit_trail(pipe, data["requests"], intents)
    validate(rows, data["requests"])
    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["case_id", "deletion_status", "steps_json"])
        writer.writeheader()
        writer.writerows(rows)

    counts = {}
    for r in rows:
        counts[r["deletion_status"]] = counts.get(r["deletion_status"], 0) + 1
    print(f"Wrote {OUTPUT.name}: {len(rows)} rows {counts}")

    if use_llm:      # only a real model run is "the final run" the usage report describes
        from evaluation.usage_report import write_report
        write_report(n_requests=len(data["requests"]))
        print("Updated code/evaluation/usage_report.md")


if __name__ == "__main__":
    main(use_llm="--no-llm" not in sys.argv)
