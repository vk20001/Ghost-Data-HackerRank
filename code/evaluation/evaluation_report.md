# Evaluation Report

Generated 2026-10-24T20:36:52+00:00 by `python3 code/evaluation/report.py`.

## How we evaluate

- **Labeled set:** the 32 public samples (`sample_deletion_requests.csv` / `sample_output.csv`). No sample answer is used by the pipeline; they are only compared against at the end.
- **Replay, not re-query:** model answers (intent and vision) are replayed from the LLM cache written by the submission run, so the scores below describe exactly the answers that produced `output.csv`, and re-running the evaluation costs no tokens.
- **Ablation:** the same samples are also scored with the deterministic fallback only (keyword intent, no vision), to show what the models add.

Metric definitions:

- *Status accuracy*: `deletion_status` matches.
- *Step precision / recall / F1*: whole steps (phase, system, resource, operation, state, evidence) matched as a multiset, ignoring position.
- *Field accuracy*: per field, share of steps that match at the same position.
- *Sequence accuracy*: share of cases whose entire `steps_json` array matches exactly, in order.

## Case, step and sequence accuracy

| Configuration | Cases | Status accuracy | Step precision | Step recall | Step F1 | Sequence accuracy |
|---|---|---|---|---|---|---|
| Submission (LLM intent + vision) | 32 | 100.0% | 0.995 | 0.969 | 0.982 | 81.2% |
| Deterministic fallback only | 32 | 96.9% | 0.989 | 0.948 | 0.968 | 78.1% |

## Field-level accuracy (submission configuration)

| Field | Accuracy |
|---|---|
| `step_no` | 97.4% |
| `phase` | 97.4% |
| `system_id` | 93.8% |
| `resource_id` | 93.3% |
| `operation` | 93.8% |
| `state` | 93.8% |
| `evidence_id` | 96.9% |

## Custom evaluation cases

Written by us to cover failure modes the brief warns about. Each builds synthetic evidence with a known answer and runs the real policy engine and planner (`code/evaluation/custom_cases.py`).

| # | Case | What it checks | Result |
|---|---|---|---|
| 1 | `active_sync_recreates_search_entry` | Account is gone, but an enabled connector keeps writing the person into search. Expected: INCOMPLETE, and the connector must be disabled BEFORE the search entry is deleted, or the next sync recreates it. | PASS |
| 2 | `later_evidence_must_not_change_the_answer` | At audit time the search entry still existed; it was only deleted the day AFTER audit_at. The later 'deleted' evidence must be ignored, so the case stays INCOMPLETE. | PASS |
| 3 | `compliant_case_with_retention_and_blocked_backup` | Everything is already in its required state: account absent, security audit record retained, tombstone keeps only the user_id, analytics anonymized, backup restore blocked. Expected: COMPLETE with evidence-backed verification only, and nothing deleted that policy says to keep. | PASS |
| 4 | `tombstone_still_shows_contact_details_and_restore_is_open` | The account was deleted, but its tombstone still holds the email, and a backup can still be restored. Expected: block the restore first, then redact the tombstone; the stable user_id may stay. | PASS |
| 5 | `negated_and_non_deletion_requests` | Requests that mention deletion but do not authorize it must be NOT_APPLICABLE; real deletion requests in several languages must not be missed. (Checks the deterministic fallback classifier; the LLM classifier is scored on the labeled samples.) | PASS |

5/5 custom cases pass.

## Failures found and improvements made

Every change below came from reading a per-case diff produced by `code/evaluation/main.py`.

| Stage | Status | Step F1 | Sequence | What failed / what changed |
|---|---|---|---|---|
| 1. First end-to-end run (rules intent, identifier linking only) | 84.4% | 0.874 | 37.5% | Baseline. |
| 2. Intent fixes, restore/export state fields, decoy accounts, later-known records | 96.9% | 0.944 | 56.3% | French 'suppression' and negated requests were misread; restore eligibility and export availability were ignored; an old failed lookup for the same email was linked as a second account. |
| 3. Activity evidence (catalog links, connector/restore checks) | 96.9% | 0.941 | 53.1% | Regression: catalog-only links pulled in a tombstone with no observed state. |
| 4. Ignore link-only records with no observed state | 96.9% | 0.944 | 56.3% | Regression fixed. |
| 5. Link anonymized analytics through their anonymization job | 96.9% | 0.932 | 50.0% | First attempt (wide window, all cases) assigned records to the wrong cases. |
| 6. Restrict to fully remediated cases and a 45-minute window | 96.9% | 0.942 | 59.4% | Precision over recall: only cases whose remediation finished compete for anonymized records. |
| 7. Deterministic case-order tie-break for identical candidate sets | 96.9% | 0.958 | 68.8% | Same rule as for accounts, so ties are resolved reproducibly. |
| 8. LLM intent + vision extraction (Gemini), attachment and catalog-claim fixes | 100% | see below | see below | Vision exposed a PDF attachment misread as a message, and a catalog link contradicted by the screenshot itself (marketing_001 shows a different person); catalog links are now treated as claims. |
| 9. Cite a covering snapshot as proof of absence | 100% | 0.982 | 81.3% | A sanity check on the 518 test outputs found 77 COMPLETE cases with an uncited verification step, which the contract forbids. A deleted account leaves nothing to observe, but a snapshot of the whole account system taken before audit_at that lacks the row proves it was absent. Uncited COMPLETE steps fell from 77 to 2. |

Current submission configuration: status 100.0%, step F1 0.982, sequence 81.2%.

## Overfitting check

The rules were developed while reading the 32 samples, so there is no clean holdout. To check whether results hinge on hand-picked values, each tuned threshold was perturbed and the 518 test outputs were compared with the baseline (measured with the cached model answers):

| Perturbation | Test rows changed | Test statuses changed | Sample step F1 |
|---|---|---|---|
| Account-job window 2h -> 3h | 25 / 518 | 0 | 0.982 (unchanged) |
| Account-job window 2h -> 1.5h | 24 / 518 | 0 | 0.982 (unchanged) |
| Anonymized-record window 45m -> 60m | 0 / 518 | 0 | 0.982 (unchanged) |
| Anonymized-record window 45m -> 35m | 0 / 518 | 0 | 0.982 (unchanged) |

Statuses never move, and the anonymized-record window is not knife-edge. The account window changes which account is cited in about 5% of cases, all of them cases that already depend on a tie-break and are listed in `review_queue.csv`. Status and policy decisions come from `deletion_rules.csv` and observed evidence, not from sample-fitted values; the step-level detail of tie-break cases is the part most likely to score lower on hidden cases.

## Remaining failures

- Two expected audit records (case_18, case_20) appear in no participant file, so no evidence-based method can produce them.
- Deleted accounts with no identifiers left are linked through the deletion job's timing and result; when several identical requests share a window, a deterministic case-order tie-break can pick the wrong account (case_23).
- Anonymized analytics records are linked the same way and are only assigned when the match is unambiguous, so some are left out (case_25, case_26).
- The case-order tie-break (used only when several cases have identical candidate sets) was confirmed on just two or three sample cases; it is a reproducible convention, not evidence.
- About 30% of deletion cases depend on a tie-break when linking an identifier-free account or anonymized record; these are listed in review_queue.csv rather than hidden.

Per-case differences (missing = expected but not produced, extra = produced but not expected):

```
case_04: predicted COMPLETE, expected COMPLETE
   missing  ('VERIFICATION', 'account_service', 'account_004', 'VERIFY_RECORD_ABSENT', 'ABSENT', 'evidence_54')
case_18: predicted INCOMPLETE, expected INCOMPLETE
   missing  ('VERIFICATION', 'security_service', 'audit_record_6645', 'VERIFY_RECORD_PRESENT', 'PRESENT', '')
case_20: predicted INCOMPLETE, expected INCOMPLETE
   missing  ('VERIFICATION', 'security_service', 'audit_record_5569', 'VERIFY_RECORD_PRESENT', 'PRESENT', '')
case_23: predicted INCOMPLETE, expected INCOMPLETE
   missing  ('VERIFICATION', 'account_service', 'account_410', 'VERIFY_RECORD_ABSENT', 'ABSENT', '')
   extra    ('VERIFICATION', 'account_service', 'account_7015', 'VERIFY_RECORD_ABSENT', 'ABSENT', '')
case_25: predicted COMPLETE, expected COMPLETE
   missing  ('VERIFICATION', 'analytics_platform', 'analytics_1895', 'VERIFY_RECORD_ANONYMIZED', 'ANONYMIZED', 'evidence_92')
case_26: predicted COMPLETE, expected COMPLETE
   missing  ('VERIFICATION', 'analytics_platform', 'analytics_4566', 'VERIFY_RECORD_ANONYMIZED', 'ANONYMIZED', 'evidence_92')
```
