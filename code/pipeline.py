"""The audit pipeline: wires the steps together for every request.

    load -> resolve identity -> parse evidence -> link records -> classify intent
         -> assess each record against policy at audit_at -> plan steps
"""

from evidence import FILE_COVERAGE, parse_activity, parse_all
from identities import IdentityIndex
from intent import rule_based
from linking import Linker
from loaders import load_all
from planner import plan, to_json
from policy import assess, load_policy


class AuditPipeline:
    def __init__(self, data=None, extra_observations=None):
        self.data = data or load_all()
        self.index = IdentityIndex(self.data["identities"], self.data["audit_log"])
        observations, _ = parse_all(self.data["evidence_files"])
        self.observations = observations + parse_activity(self.data["activity"]) + (extra_observations or [])
        self.captured_at = {f["file_id"]: f["captured_at"] for f in self.data["evidence_files"]}
        self.captured_at.update({a["activity_id"]: a["event_time"] for a in self.data["activity"]})
        self.policy = load_policy(self.data["rules"])
        self.linker = Linker(self.index, self.observations, self.data["activity"])
        # Account linking needs every request at once (cases compete for accounts).
        all_requests = self.data["requests"] + self.data["sample_requests"]
        self.linker.assign_accounts(all_requests)
        # Anonymized records with no identifiers left can only belong to a case whose
        # remediation actually ran to the end, so only fully compliant deletion cases
        # compete for them (first pass decides which cases those are).
        finished = [r for r in all_requests
                    if rule_based(r["request_text"]) and self.audit(r, True)[0] == "COMPLETE"]
        self.linker.assign_orphans(finished)

    def audit(self, request, is_deletion=None):
        """Audit one request. Returns (status, steps, trace) where trace explains the decision."""
        if is_deletion is None:
            is_deletion = rule_based(request["request_text"])
        if not is_deletion:
            return "NOT_APPLICABLE", [], {"reason": "request does not authorize deletion"}

        person = self.index.resolve(request["email"])
        account = self.linker.account_for_case.get(request["case_id"])
        # Keep only the account chosen for this request; other accounts that merely
        # appeared in a failed lookup for the same email are not this person's account.
        resources = {r for r in self.linker.resources_for(person) if not r.startswith("account_")}
        if account:
            resources.add(account)
        resources |= set(self.linker.orphans_for_case.get(request["case_id"], []))

        records = []
        for rid in sorted(resources):
            obs = self.linker.by_resource.get(rid, [])
            if rid != account and not any(o.present is not None for o in obs):
                continue        # only a catalog link, never an observed record
            state = assess(rid, self.linker.by_resource.get(rid, []), self.policy,
                           request["audit_at"], self.captured_at)
            if state is None:
                continue
            if state.system_id is None:
                state.system_id = "account_service"
            records.append(state)

        self._prove_absence(records, request["audit_at"])
        status, steps = plan(records)
        case_id = request["case_id"]
        trace = {
            "case_id": case_id,
            "user_id": person["user_id"],
            "identifiers_searched": len(person["emails"]) + len(person["phones"]) + len(person["hashes"]) + 1,
            "account": account,
            "account_link": self.linker.link_method.get(case_id),
            "anonymized_record_link": self.linker.orphan_method.get(case_id),
            "status": status,
            "records": [{"resource_id": r.resource_id, "category": r.category, "required_state": r.required,
                         "compliant_at_audit": r.compliant, "finding": r.finding, "evidence_id": r.evidence_id,
                         "observed_before_audit": r.observed} for r in records],
        }
        return status, steps, trace

    def run(self, requests, intents=None):
        rows = []
        for r in sorted(requests, key=lambda r: int(r["case_id"].split("_")[1])):
            is_del = intents.get(r["case_id"]) if intents else None
            status, steps, _ = self.audit(r, is_del)
            rows.append({"case_id": r["case_id"], "deletion_status": status, "steps_json": to_json(steps)})
        return rows

    def _prove_absence(self, records, audit_at):
        """Cite a covering snapshot for records we never observed but must prove absent.

        A deleted account usually leaves nothing to observe. If a snapshot of the whole
        account system, taken at or before audit_at, is one of the files this case's own
        evidence comes from (or the latest such snapshot when the case has none), and the
        account is not in it, that snapshot proves the account was absent.
        """
        case_files = {r.evidence_id for r in records if r.evidence_id}
        for r in records:
            if r.observed or r.required != "ABSENT":
                continue
            covering = [(when, fid) for fid, systems in FILE_COVERAGE.items()
                        for sys_id, when in systems.items()
                        if sys_id == r.system_id and when <= audit_at
                        and self.captured_at.get(fid, when) <= audit_at]
            if not covering:
                continue
            same_case = [c for c in covering if c[1] in case_files]
            when, fid = max(same_case or covering)
            r.evidence_id, r.observed = fid, True
