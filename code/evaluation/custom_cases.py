"""Custom evaluation cases written by us, beyond the 32 public samples.

Each case builds a small synthetic situation (observations with known truth),
runs the same policy engine and planner the pipeline uses, and checks the
status and the exact ordered steps. They target the failure modes the brief
warns about: recreation by sync jobs, restore paths, look-alike evidence,
evidence created after audit_at, tombstones, and non-deletion requests.

Run: python3 code/evaluation/custom_cases.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evidence import Observation, extract_identifiers, system_for   # noqa: E402
from intent import rule_based                                       # noqa: E402
from loaders import load_csv                                        # noqa: E402
from planner import plan                                            # noqa: E402
from policy import assess, load_policy                              # noqa: E402

POLICY = load_policy(load_csv("deletion_rules.csv"))
T = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)  # noqa: E731


def obs(file_id, rid, when, present, **attrs):
    return Observation(file_id, system_for(rid), rid, T(when), present, "custom", attrs,
                       extract_identifiers(attrs))


def run_case(records, audit_at):
    """records: {resource_id: [Observation, ...]} -> (status, steps as tuples)."""
    captured = {o.file_id: o.observed_at for os_ in records.values() for o in os_}
    states = [assess(rid, os_, POLICY, T(audit_at), captured) for rid, os_ in sorted(records.items())]
    status, steps = plan([s for s in states if s])
    return status, [(s["phase"], s["resource_id"], s["operation"], s["evidence_id"]) for s in steps]


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def active_sync_recreates_search_entry():
    """Account is gone, but an enabled connector keeps writing the person into search.
    Expected: INCOMPLETE, and the connector must be disabled BEFORE the search entry is
    deleted, or the next sync recreates it."""
    records = {
        "account_9001": [obs("ev_a", "account_9001", "2026-09-10 10:00", False)],
        "search_9002": [obs("ev_b", "search_9002", "2026-09-11 09:00", True, email="ghost@example.com")],
        "data_flow_9003": [obs("ev_b", "data_flow_9003", "2026-09-11 09:00", True,
                               customer_email="ghost@example.com", enabled="true")],
    }
    expected = ("INCOMPLETE", [
        ("FINDING", "search_9002", "REPORT_REMAINING_RECORD", "ev_b"),
        ("FINDING", "data_flow_9003", "REPORT_ACTIVE_DATA_FLOW", "ev_b"),
        ("REMEDIATION", "data_flow_9003", "DISABLE_DATA_FLOW", ""),
        ("REMEDIATION", "search_9002", "DELETE_RECORD", ""),
        ("VERIFICATION", "account_9001", "VERIFY_RECORD_ABSENT", ""),
        ("VERIFICATION", "search_9002", "VERIFY_RECORD_ABSENT", ""),
        ("VERIFICATION", "data_flow_9003", "VERIFY_DATA_FLOW_DISABLED", ""),
    ])
    return run_case(records, "2026-09-12 00:00"), expected


@case
def later_evidence_must_not_change_the_answer():
    """At audit time the search entry still existed; it was only deleted the day AFTER
    audit_at. The later 'deleted' evidence must be ignored, so the case stays INCOMPLETE."""
    records = {
        "account_9101": [obs("ev_a", "account_9101", "2026-09-01 10:00", False)],
        "search_9102": [obs("ev_b", "search_9102", "2026-09-02 10:00", True, email="late@example.com"),
                        obs("ev_c", "search_9102", "2026-09-06 10:00", False)],
    }
    expected = ("INCOMPLETE", [
        ("FINDING", "search_9102", "REPORT_REMAINING_RECORD", "ev_b"),
        ("REMEDIATION", "search_9102", "DELETE_RECORD", ""),
        ("VERIFICATION", "account_9101", "VERIFY_RECORD_ABSENT", ""),
        ("VERIFICATION", "search_9102", "VERIFY_RECORD_ABSENT", ""),
    ])
    return run_case(records, "2026-09-05 00:00"), expected


@case
def compliant_case_with_retention_and_blocked_backup():
    """Everything is already in its required state: account absent, security audit record
    retained, tombstone keeps only the user_id, analytics anonymized, backup restore blocked.
    Expected: COMPLETE with evidence-backed verification only, and nothing deleted that
    policy says to keep."""
    records = {
        "account_9201": [obs("ev_a", "account_9201", "2026-09-01 10:00", False)],
        "audit_record_9202": [obs("ev_a", "audit_record_9202", "2026-09-01 10:00", True,
                                  subject_reference="user_77", event_code="ACCOUNT_DELETION_RECORDED")],
        "tombstone_9203": [obs("ev_b", "tombstone_9203", "2026-09-01 11:00", True,
                               subject_reference="user_77", name=None, email=None, phone=None)],
        "analytics_9204": [obs("ev_b", "analytics_9204", "2026-09-01 11:00", True,
                               email=None, phone=None, audience_cohort="general_shopper")],
        "backup_9205": [obs("ev_b", "backup_9205", "2026-09-01 11:00", True,
                            customer_reference="user_77", restore_allowed="false")],
    }
    expected = ("COMPLETE", [
        ("VERIFICATION", "account_9201", "VERIFY_RECORD_ABSENT", "ev_a"),
        ("VERIFICATION", "audit_record_9202", "VERIFY_RECORD_PRESENT", "ev_a"),
        ("VERIFICATION", "tombstone_9203", "VERIFY_RECORD_REDACTED", "ev_b"),
        ("VERIFICATION", "analytics_9204", "VERIFY_RECORD_ANONYMIZED", "ev_b"),
        ("VERIFICATION", "backup_9205", "VERIFY_RESTORE_BLOCKED", "ev_b"),
    ])
    return run_case(records, "2026-09-02 00:00"), expected


@case
def tombstone_still_shows_contact_details_and_restore_is_open():
    """The account was deleted, but its tombstone still holds the email, and a backup can
    still be restored. Expected: block the restore first, then redact the tombstone; the
    stable user_id may stay."""
    records = {
        "account_9301": [obs("ev_a", "account_9301", "2026-09-01 10:00", False)],
        "tombstone_9302": [obs("ev_a", "tombstone_9302", "2026-09-01 10:00", True,
                               subject_reference="user_88", email="still@example.com")],
        "backup_9303": [obs("ev_a", "backup_9303", "2026-09-01 10:00", True,
                            customer_email="still@example.com", restore_allowed="true")],
    }
    expected = ("INCOMPLETE", [
        ("FINDING", "tombstone_9302", "REPORT_EXPOSED_PII", "ev_a"),
        ("FINDING", "backup_9303", "REPORT_RESTORE_RISK", "ev_a"),
        ("REMEDIATION", "backup_9303", "BLOCK_RESTORE", ""),
        ("REMEDIATION", "tombstone_9302", "REDACT_RECORD", ""),
        ("VERIFICATION", "account_9301", "VERIFY_RECORD_ABSENT", ""),
        ("VERIFICATION", "tombstone_9302", "VERIFY_RECORD_REDACTED", ""),
        ("VERIFICATION", "backup_9303", "VERIFY_RESTORE_BLOCKED", ""),
    ])
    return run_case(records, "2026-09-02 00:00"), expected


@case
def negated_and_non_deletion_requests():
    """Requests that mention deletion but do not authorize it must be NOT_APPLICABLE;
    real deletion requests in several languages must not be missed.
    (Checks the deterministic fallback classifier; the LLM classifier is scored on the
    labeled samples.)"""
    texts = {
        "Please send me a copy of my data. I still use the account, so don't delete anything.": False,
        "Stop the newsletter please, but keep my account and orders.": False,
        "Can you correct the spelling of my surname without removing any records?": False,
        "Je demande la suppression définitive de mon compte.": True,
        "このメールアドレスに関連するアカウントの削除を依頼します。": True,
        "Bitte löschen Sie mein Konto und alle Daten.": True,
    }
    got = {t: rule_based(t) for t in texts}
    status = "PASS" if got == texts else "FAIL"
    return (status, sorted(got.items())), (status, sorted(texts.items()))


def run_all(verbose=True):
    results = []
    for fn in CASES:
        actual, expected = fn()
        ok = actual == expected
        results.append({"name": fn.__name__, "doc": " ".join(fn.__doc__.split()), "passed": ok,
                        "actual": actual, "expected": expected})
        if verbose:
            print(f"{'PASS' if ok else 'FAIL'}  {fn.__name__}")
            if not ok:
                print("   expected:", expected)
                print("   actual:  ", actual)
    return results


if __name__ == "__main__":
    res = run_all()
    print(f"\n{sum(r['passed'] for r in res)}/{len(res)} custom cases passed")
