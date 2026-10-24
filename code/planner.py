"""Step 6: turn assessed records into the ordered steps_json array.

Ordering follows problem_statement.md exactly:
phase (FINDING, REMEDIATION, VERIFICATION), then the operation order in the
"Allowed Operations" table, then system_id, then resource_id.

Remediation order also encodes the safety rule from the brief: stop data flows
and block restores BEFORE deleting or redacting, otherwise a sync could
recreate the data right after we remove it.
"""

import json

OPERATION_ORDER = [
    "REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK",
    "DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD",
    "VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED",
    "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED",
]
PHASE_ORDER = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}

STATE_OF = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT",
    "REPORT_ACTIVE_DATA_FLOW": "ACTIVE", "REPORT_RESTORE_RISK": "ACTIVE",
    "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED", "DELETE_RECORD": "ABSENT",
    "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT",
    "VERIFY_RECORD_REDACTED": "REDACTED", "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED": "DISABLED", "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}
VERIFY_FOR = {
    "ABSENT": "VERIFY_RECORD_ABSENT", "PRESENT": "VERIFY_RECORD_PRESENT",
    "REDACTED": "VERIFY_RECORD_REDACTED", "ANONYMIZED": "VERIFY_RECORD_ANONYMIZED",
    "DISABLED": "VERIFY_DATA_FLOW_DISABLED", "BLOCKED": "VERIFY_RESTORE_BLOCKED",
}
REMEDIATE_FOR = {
    "ABSENT": "DELETE_RECORD", "REDACTED": "REDACT_RECORD", "ANONYMIZED": "ANONYMIZE_RECORD",
    "DISABLED": "DISABLE_DATA_FLOW", "BLOCKED": "BLOCK_RESTORE",
}


def plan(records):
    """records: list of policy.RecordState. Returns (status, steps)."""
    incomplete = any(not r.compliant for r in records)
    steps = []
    for r in records:
        if incomplete:
            if not r.compliant:
                steps.append(("FINDING", r, r.finding, r.evidence_id))
                steps.append(("REMEDIATION", r, REMEDIATE_FOR[r.required], ""))
            steps.append(("VERIFICATION", r, VERIFY_FOR[r.required], ""))
        else:
            steps.append(("VERIFICATION", r, VERIFY_FOR[r.required], r.evidence_id))

    steps.sort(key=lambda s: (PHASE_ORDER[s[0]], OPERATION_ORDER.index(s[2]), s[1].system_id, s[1].resource_id))
    out = [{
        "step_no": i,
        "phase": phase,
        "system_id": r.system_id,
        "resource_id": r.resource_id,
        "operation": op,
        "state": STATE_OF[op],
        "evidence_id": evidence,
    } for i, (phase, r, op, evidence) in enumerate(steps, start=1)]
    return ("INCOMPLETE" if incomplete else "COMPLETE"), out


def to_json(steps):
    """Compact, deterministic serialization."""
    return json.dumps(steps, ensure_ascii=False, separators=(",", ":"))
