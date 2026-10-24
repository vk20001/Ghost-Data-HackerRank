"""Step 4: link each case to the records that belong to its subject.

Two routes:

1. Identifier match (the normal case). Any observation whose identifiers
   (email, phone, hash, user_id) overlap the subject's key set belongs to the
   subject. All observations of that resource_id then belong to the case, even
   later ones where the identifiers were already removed.

2. Account records with no identifiers left. Once an account is deleted, a
   lookup only returns "404 account_3136"; nothing names the person. The only
   remaining link is the deletion workflow itself: system_activity.csv records
   "deletion worker submitted account_3136" shortly before the workflow reported
   its status. We match on three constraints, the way an analyst would:
     - the job ran in a short window around reported_at,
     - the job's result agrees with the reported status
       (SUCCESS -> COMPLETE, PARTIAL -> PARTIALLY_COMPLETE, FAILED -> NOT_STARTED),
     - the account is not already linked to someone else.
   Then we propagate: an account claimed by one case is removed from every
   other case's candidates, which often leaves exactly one option. Cases with
   identical candidate sets are paired in case order with job order as a final,
   deterministic tie-break.
"""

from collections import defaultdict
from datetime import timedelta

STATUS_TO_JOB_RESULT = {
    "COMPLETE": "SUCCESS",
    "PARTIALLY_COMPLETE": "PARTIAL",
    "NOT_STARTED": "FAILED",
}
WINDOW_BEFORE = timedelta(hours=2)
WINDOW_AFTER = timedelta(minutes=30)
ORPHAN_WINDOW = timedelta(minutes=45)


def subject_keys(person):
    """All identifier tokens for one person, in the same format evidence.py emits."""
    return (set(person["emails"]) | {person["user_id"]} | set(person["hashes"])
            | {"phone:" + p for p in person["phones"]})


class Linker:
    def __init__(self, index, observations, activity):
        self.index = index
        self.by_resource = defaultdict(list)
        for o in observations:
            self.by_resource[o.resource_id].append(o)

        # Which user(s) each resource is tied to through identifiers.
        key_to_user = {}
        for uid, person in index.users.items():
            for k in subject_keys(person):
                key_to_user[k] = uid
        self.resource_users = defaultdict(set)
        catalog_users = defaultdict(set)
        for o in observations:
            target = catalog_users if o.attrs.get("_link_only") else self.resource_users
            for k in o.identifiers:
                if k in key_to_user:
                    target[o.resource_id].add(key_to_user[k])

        # A data-catalog entry is metadata, i.e. a claim. If the record itself was
        # observed showing direct identifiers and none of them belong to the cataloged
        # person, the observed record wins and the catalog link is ignored.
        for rid, users in catalog_users.items():
            shown = {k for o in self.by_resource.get(rid, []) if not o.attrs.get("_link_only")
                     for k in o.identifiers if "@" in k or k.startswith("phone:")}
            for uid in users:
                own = subject_keys(index.users[uid])
                if not shown or shown & own:
                    self.resource_users[rid].add(uid)

        self.activity = activity
        self.orphans_for_case = {}
        self.account_jobs = [a for a in activity
                             if a["target_system"] == "account_service" and a["event_type"] == "DELETE_RECORD"]
        self.account_for_case = {}

    def resources_for(self, person):
        """Resources linked to a person through identifiers."""
        uid = person["user_id"]
        return sorted(r for r, users in self.resource_users.items() if uid in users)

    def assign_accounts(self, requests):
        """Decide the account resource for every request (see module docstring)."""
        assigned, candidates = {}, {}
        for r in requests:
            person = self.index.resolve(r["email"])
            direct = [res for res in self.resources_for(person) if res.startswith("account_")]
            if len(direct) > 1:
                # Several accounts were looked up with this person's email. Prefer the one the
                # deletion workflow actually processed for this request, then the most recent.
                job_gap = {a["resource_id"]: abs((a["event_time"] - r["reported_at"]).total_seconds())
                           for a in self.account_jobs if a["resource_id"] in direct}
                latest = {res: max(o.observed_at for o in self.by_resource[res]) for res in direct}
                direct.sort(key=lambda res: (job_gap.get(res, float("inf")), -latest[res].timestamp()))
            if direct:
                assigned[r["case_id"]] = direct[0]
                continue
            lo, hi = r["reported_at"] - WINDOW_BEFORE, r["reported_at"] + WINDOW_AFTER
            in_window = [a for a in self.account_jobs
                         if lo <= a["event_time"] <= hi and not self.resource_users.get(a["resource_id"])]
            expected = STATUS_TO_JOB_RESULT.get(r["reported_status"])
            matching = [a for a in in_window if a["reported_result"] == expected]
            candidates[r["case_id"]] = {a["resource_id"] for a in (matching or in_window)}

        job_time = {a["resource_id"]: a["event_time"] for a in self.account_jobs}

        def propagate():
            taken = set(assigned.values())
            changed = True
            while changed:
                changed = False
                for case_id in list(candidates):
                    candidates[case_id] -= taken
                    if len(candidates[case_id]) == 1:
                        assigned[case_id] = candidates.pop(case_id).pop()
                        taken.add(assigned[case_id])
                        changed = True

        propagate()
        groups = defaultdict(list)
        for case_id, cands in candidates.items():
            groups[frozenset(cands)].append(case_id)
        for cands, case_ids in groups.items():
            if cands and len(cands) == len(case_ids):
                ordered_cases = sorted(case_ids, key=lambda c: int(c.split("_")[1]))
                ordered_accounts = sorted(cands, key=lambda a: job_time[a])
                for case_id, account in zip(ordered_cases, ordered_accounts):
                    assigned[case_id] = account
                    candidates.pop(case_id)
        propagate()
        for case_id, cands in candidates.items():          # last resort: earliest job
            if cands:
                assigned[case_id] = min(cands, key=lambda a: job_time[a])
        self.account_for_case = assigned
        return assigned


    def assign_orphans(self, requests, prefix="analytics_", event_type="ANONYMIZE_RECORD"):
        """Link records whose identifiers were already removed (e.g. anonymized analytics).

        After anonymization nothing in the record names the person; the only trace is
        the policy job that anonymized it. Same reasoning as accounts: the job ran inside
        the request's window, the record is not tied to anyone else, and each record
        belongs to one case. Ties are broken by pairing cases in case order with
        jobs in job order, the same deterministic rule used for accounts.
        """
        jobs = {}
        for a in self.activity:
            rid = a["resource_id"]
            if (a["event_type"] == event_type and rid.startswith(prefix)
                    and not self.resource_users.get(rid) and self.by_resource.get(rid)):
                jobs.setdefault(rid, a["event_time"])
        candidates = {}
        for r in requests:
            person = self.index.resolve(r["email"])
            if any(res.startswith(prefix) for res in self.resources_for(person)):
                continue
            lo, hi = r["reported_at"] - ORPHAN_WINDOW, r["reported_at"]
            cands = {rid for rid, t in jobs.items() if lo <= t <= hi}
            if cands:
                candidates[r["case_id"]] = cands
        assigned = {}

        def propagate():
            changed = True
            while changed:
                changed = False
                taken = set(assigned.values())
                for case_id in list(candidates):
                    candidates[case_id] -= taken
                    if len(candidates[case_id]) == 1:
                        assigned[case_id] = candidates.pop(case_id).pop()
                        changed = True
                    elif not candidates[case_id]:
                        candidates.pop(case_id)

        propagate()
        groups = {}
        for case_id, cands in candidates.items():
            groups.setdefault(frozenset(cands), []).append(case_id)
        for cands, case_ids in groups.items():
            if len(cands) == len(case_ids):
                order = sorted(case_ids, key=lambda c: int(c.split("_")[1]))
                for case_id, rid in zip(order, sorted(cands, key=lambda x: jobs[x])):
                    assigned[case_id] = rid
                    candidates.pop(case_id)
        propagate()
        self.orphans_for_case = {}
        for case_id, rid in assigned.items():
            self.orphans_for_case.setdefault(case_id, []).append(rid)
        return assigned
