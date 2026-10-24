"""Step 1: load the tabular inputs from dataset/ as-is.

This is the raw layer: read every CSV, parse timestamps, and apply no business
logic. Everything later in the pipeline reads from these structures.
"""

import csv
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = REPO_ROOT / "dataset"

# Columns that hold ISO-8601 timestamps, per file.
TIMESTAMP_COLUMNS = {
    "deletion_requests.csv": ["requested_at", "reported_at", "audit_at"],
    "sample_deletion_requests.csv": ["requested_at", "reported_at", "audit_at"],
    "user_identities.csv": ["updated_at"],
    "user_audit_log.csv": ["event_time"],
    "system_activity.csv": ["event_time"],
    "evidence_files.csv": ["captured_at"],
}


def parse_ts(value):
    """Parse '2026-09-15T12:00:00Z' into a timezone-aware UTC datetime."""
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def load_csv(filename, dataset_dir=DATASET_DIR):
    """Read one CSV into a list of dicts, converting known timestamp columns."""
    ts_cols = TIMESTAMP_COLUMNS.get(filename, [])
    with open(dataset_dir / filename, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        for col in ts_cols:
            row[col] = parse_ts(row[col])
    return rows


def load_all(dataset_dir=DATASET_DIR):
    """Load every tabular input the pipeline needs."""
    return {
        "requests": load_csv("deletion_requests.csv", dataset_dir),
        "sample_requests": load_csv("sample_deletion_requests.csv", dataset_dir),
        "sample_output": load_csv("sample_output.csv", dataset_dir),
        "identities": load_csv("user_identities.csv", dataset_dir),
        "audit_log": load_csv("user_audit_log.csv", dataset_dir),
        "activity": load_csv("system_activity.csv", dataset_dir),
        "rules": load_csv("deletion_rules.csv", dataset_dir),
        "evidence_files": load_csv("evidence_files.csv", dataset_dir),
    }


if __name__ == "__main__":
    data = load_all()
    for name, rows in data.items():
        print(f"{name:16} {len(rows):5} rows")
