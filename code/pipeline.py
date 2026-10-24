"""The audit pipeline: wires the steps together for every request.

    load -> resolve identity -> parse evidence -> link records -> classify intent
         -> assess each record against policy at audit_at -> plan steps
"""

from evidence import parse_all
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
        self.observations = observations + (extra_observations or [])
        self.captured_at = {f["file_id"]: f["captured_at"] for f in self.data["evidence_files"]}
        self.policy = load_policy(self.data["rules"])
        self.linker = Linker(self.index, self.observations, self.data["activity"])
        # Account linking needs every request at once (cases compete for accounts).
        self.linker.assign_accounts(self.data["requests"] + self.data["sample_requests"])

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

        records = []
        for rid in sorted(resources):
            state = assess(rid, self.linker.by_resource.get(rid, []), self.policy,
                           request["audit_at"], self.captured_at)
            if state is None:
                continue
            if state.system_id is None:
                state.system_id = "account_service"
            records.append(state)

        status, steps = plan(records)
        trace = {"user_id": person["user_id"], "account": account,
                 "records": [(r.resource_id, r.category, r.required, r.compliant, r.evidence_id)
                             for r in records]}
        return status, steps, trace

    def run(self, requests, intents=None):
        rows = []
        for r in sorted(requests, key=lambda r: int(r["case_id"].split("_")[1])):
            is_del = intents.get(r["case_id"]) if intents else None
            status, steps, _ = self.audit(r, is_del)
            rows.append({"case_id": r["case_id"], "deletion_status": status, "steps_json": to_json(steps)})
        return rows
