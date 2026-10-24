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
    decisions = classify_all(data["requests"], use_llm=use_llm)
    sources = {}
    for _, src in decisions.values():
        sources[src] = sources.get(src, 0) + 1
    print(f"  decided by: {sources}")
    intents = {cid: is_del for cid, (is_del, _) in decisions.items()}

    rows = pipe.run(data["requests"], intents)
    validate(rows, data["requests"])
    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["case_id", "deletion_status", "steps_json"])
        writer.writeheader()
        writer.writerows(rows)

    counts = {}
    for r in rows:
        counts[r["deletion_status"]] = counts.get(r["deletion_status"], 0) + 1
    print(f"Wrote {OUTPUT.name}: {len(rows)} rows {counts}")

    from evaluation.usage_report import write_report
    write_report(n_requests=len(data["requests"]))
    print("Updated code/evaluation/usage_report.md")


if __name__ == "__main__":
    main(use_llm="--no-llm" not in sys.argv)
