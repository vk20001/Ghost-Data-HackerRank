"""Step 5: the policy engine.

For one case: classify each linked record into a data category, look up the
required treatment in deletion_rules.csv, reconstruct the record's state at
audit_at from the latest evidence, and compare the two.

The rules file is the single source of truth for treatments; this module only
decides which category a record belongs to and what its observed state is.
"""

from dataclasses import dataclass

# Records whose exposed fields count as direct identifiers.
NAME_FIELDS = {"name", "full_name", "requester_name", "display_name"}
NULLS = {None, "", "null", "None", "no direct identifier"}


def is_null(value):
    return not isinstance(value, (list, dict)) and value in NULLS

# deletion_rules.csv action -> the end state the record must reach.
ACTION_TARGET = {"DELETE": "ABSENT", "REDACT": "REDACTED", "ANONYMIZE": "ANONYMIZED", "RETAIN": "PRESENT"}


def truthy(value):
    return str(value).strip().lower() in {"1", "true", "yes", "active"}


def category_for(resource_id, observations):
    """Map a record to a deletion_rules.csv data category using its own content."""
    attrs = {}
    for o in observations:
        attrs.update({k: v for k, v in o.attrs.items() if not is_null(v)})
    rtype = attrs.get("record_type")

    if resource_id.startswith("account_"):
        return "USER_PROFILE"
    if resource_id.startswith("search_"):
        return "SEARCH_INDEX_ENTRY"
    if resource_id.startswith("cache_"):
        return "CACHE_ENTRY"
    if resource_id.startswith("export_"):
        return "USER_EXPORT"
    if resource_id.startswith("marketing_"):
        return "MARKETING_PROFILE"
    if resource_id.startswith("email_delivery_"):
        return "EMAIL_DELIVERY_LOG"
    if resource_id.startswith("audit_record_"):
        return "SECURITY_AUDIT_LOG"
    if resource_id.startswith("legal_hold_"):
        return "LEGAL_HOLD_RECORD"
    if resource_id.startswith("backup_"):
        return "BACKUP_ARCHIVE"
    if resource_id.startswith("data_flow_"):
        return "DATA_FLOW"
    if resource_id.startswith("tombstone_"):
        return "ACCOUNT_TOMBSTONE"
    if resource_id.startswith("analytics_"):
        # A marketing contact stored in the analytics platform is still a marketing profile.
        if rtype == "MARKETING_PROFILE" or "email_opt_in" in attrs or str(attrs.get("event", "")).startswith("contact_"):
            return "MARKETING_PROFILE"
        return "ANALYTICS_PROFILE"
    if resource_id.startswith("support_"):
        if (rtype == "SUPPORT_ATTACHMENT" or "file_name" in attrs or "file" in attrs
                or attrs.get("_collection") == "ticket_attachments"
                or "attachment" in str(attrs.get("event", ""))):
            return "SUPPORT_ATTACHMENT"
        if attrs.get("service") == "support_replica" or "contact_query" in str(attrs.get("event", "")):
            return "SECONDARY_IDENTIFIER"     # a copy of contact data, not a shared conversation
        return "SUPPORT_MESSAGE"
    return None


def load_policy(rules):
    """data_category -> required action, straight from deletion_rules.csv."""
    return {r["data_category"]: r["required_action"] for r in rules}


@dataclass
class RecordState:
    resource_id: str
    system_id: str
    category: str
    required: str          # ABSENT / REDACTED / ANONYMIZED / PRESENT / DISABLED / BLOCKED
    compliant: bool
    finding: str           # finding operation if not compliant, else None
    evidence_id: str       # evidence proving the current state
    observed: bool         # False if no evidence at or before audit_at


def exposes_direct_identifiers(obs, allow_user_ref):
    """Does this observation still show identifiers that policy says must go?"""
    if str(obs.attrs.get("result", "")).upper() == "IDENTIFIABLE":
        return True
    if str(obs.attrs.get("result", "")).upper() in {"ANONYMIZED", "REDACTED"}:
        return False
    ids = {i for i in obs.identifiers if not (allow_user_ref and i.startswith("user_"))}
    if ids:
        return True
    return any(not is_null(obs.attrs.get(f)) for f in NAME_FIELDS)


def flow_enabled(obs):
    result = str(obs.attrs.get("result", "")).upper()
    if result in {"ACTIVE", "DISABLED"}:
        return result == "ACTIVE"
    return truthy(obs.attrs.get("enabled", "false"))


def restore_allowed(obs):
    a = obs.attrs
    result = str(a.get("result", "")).upper()
    if result in {"ACTIVE", "BLOCKED"}:
        return result == "ACTIVE"
    if "decision" in a:                                   # restore authorization check
        return str(a["decision"]).lower() != "deny"
    if "eligible" in a:                                   # restore candidate evaluation
        return truthy(a["eligible"]) and not truthy(a.get("deletion_block", "false"))
    return truthy(a.get("restore_allowed", "false"))


def export_available(obs):
    for key in ("download_available", "available"):
        if key in obs.attrs:
            return truthy(obs.attrs[key])
    return True


def assess(resource_id, observations, policy, audit_at, captured_at):
    """Reconstruct a record's state at audit_at and compare it with policy."""
    usable = [o for o in observations
              if o.observed_at <= audit_at and captured_at[o.file_id] <= audit_at]
    category = category_for(resource_id, observations)
    system_id = observations[0].system_id if observations else None
    if category is None:
        return None

    if category == "DATA_FLOW":
        required = "DISABLED"
    elif category == "BACKUP_ARCHIVE":
        required = "BLOCKED"
    else:
        required = ACTION_TARGET[policy[category]]

    # Link-only evidence (e.g. a catalog entry) ties a record to a person but says
    # nothing about its current state, so it never counts as the latest state.
    usable = [o for o in usable if o.present is not None]
    if not usable:
        return RecordState(resource_id, system_id, category, required, True, None, "", False)

    latest = max(usable, key=lambda o: (o.observed_at, captured_at[o.file_id]))
    present = latest.present is not False

    if category == "USER_EXPORT" and present:
        present = export_available(latest)                # an expired export link is gone
    if required == "ABSENT":
        compliant, finding = not present, "REPORT_REMAINING_RECORD"
    elif required == "PRESENT":
        compliant, finding = present, None
    elif required == "DISABLED":
        compliant, finding = not (present and flow_enabled(latest)), "REPORT_ACTIVE_DATA_FLOW"
    elif required == "BLOCKED":
        compliant, finding = not (present and restore_allowed(latest)), "REPORT_RESTORE_RISK"
    else:  # REDACTED or ANONYMIZED
        allow_ref = required == "REDACTED"
        compliant = not (present and exposes_direct_identifiers(latest, allow_ref))
        finding = "REPORT_EXPOSED_PII"
    return RecordState(resource_id, system_id, category, required, compliant,
                       None if compliant else finding, latest.file_id, True)
