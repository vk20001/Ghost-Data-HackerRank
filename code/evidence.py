"""Step 3: parse every text-based evidence file into one observation table.

Each source format gets a small parser, and every parser emits the same shape:

    Observation(file_id, system_id, resource_id, observed_at, present, attrs, identifiers)

- present:     True if the record was seen, False if a lookup proved it absent,
               None if the source says nothing either way.
- attrs:       the raw fields from the source (state flags such as enabled,
               restore_allowed, result, http_status live here).
- identifiers: normalized personal keys found in the record (emails, user refs,
               phone digits, email hashes). Later steps match these against a
               person's key set from identities.py.

Images and PDFs are handled in a separate step (vision), which writes into the
same shape.
"""

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from identities import norm_email, norm_phone
from loaders import DATASET_DIR, parse_ts

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
USER_REF_RE = re.compile(r"\buser_\d+\b")
HASH_RE = re.compile(r"\b[0-9a-f]{64}\b")
KV_RE = re.compile(r'([A-Za-z_][\w.]*)=("(?:[^"\\]|\\.)*"|\S+)')
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s*(.*)$")

# The resource_id prefix tells us which system owns a record. This is more
# reliable than the logging service name (e.g. "profile_service" logs about
# account_service records).
PREFIX_TO_SYSTEM = [
    ("account_", "account_service"),
    ("search_", "search_service"),
    ("analytics_", "analytics_platform"),
    ("support_", "support_platform"),
    ("export_", "export_service"),
    ("data_flow_", "integration_service"),
    ("backup_", "backup_service"),
    ("audit_record_", "security_service"),
    ("tombstone_", "identity_service"),
    ("legal_hold_", "legal_service"),
    ("cache_", "cache_service"),
    ("marketing_", "marketing_platform"),
    ("email_delivery_", "email_delivery_service"),
    ("telemetry_", "product_telemetry"),
]

# Fields whose values are phone numbers (only these are parsed as phones, so
# order totals or IDs are never mistaken for phone numbers).
PHONE_FIELDS = {"phone", "owner_phone", "requester_phone", "customer_phone"}


def system_for(resource_id):
    for prefix, system in PREFIX_TO_SYSTEM:
        if resource_id.startswith(prefix):
            return system
    return None


@dataclass
class Observation:
    file_id: str
    system_id: str
    resource_id: str
    observed_at: datetime
    present: object                     # True / False / None
    source: str                         # parser that produced it, for debugging
    attrs: dict = field(default_factory=dict)
    identifiers: set = field(default_factory=set)


def extract_identifiers(value, key=None, out=None):
    """Walk any value (str, dict, list) and collect normalized personal keys."""
    out = set() if out is None else out
    if isinstance(value, dict):
        for k, v in value.items():
            extract_identifiers(v, k, out)
    elif isinstance(value, list):
        for v in value:
            extract_identifiers(v, key, out)
    elif isinstance(value, str):
        for e in EMAIL_RE.findall(value):
            out.add(norm_email(e))
        out.update(USER_REF_RE.findall(value))
        out.update(h.lower() for h in HASH_RE.findall(value.lower()))
        if key in PHONE_FIELDS:
            digits = norm_phone(value)
            if digits and len(digits) >= 7:
                out.add("phone:" + digits)
    return out


def make_obs(file_id, resource_id, observed_at, present, source, attrs, system_id=None):
    system_id = system_id or system_for(resource_id)
    if not system_id or not resource_id:
        return None
    return Observation(file_id, system_id, resource_id, observed_at, present, source,
                       attrs, extract_identifiers(attrs))


def present_from_result(result):
    """Map a reported lookup result to presence."""
    if result is None:
        return None
    r = str(result).upper()
    if r in {"NOT_FOUND", "ABSENT", "MISS", "DELETED"}:
        return False
    return True


# ---------------------------------------------------------------- SQL snapshots

def parse_sql(file_id, path):
    """Execute the snapshot in an in-memory SQLite database and read every row."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(path.read_text(encoding="utf-8"))
    observations = []
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        if table == "snapshot_scope":
            continue
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        for row in conn.execute(f'SELECT * FROM "{table}"'):
            rec = dict(zip(cols, row))
            if "record_json" in rec:                     # generic replica table
                attrs = json.loads(rec["record_json"])
                system = rec.get("origin_system")
            else:                                        # typed inventory table
                attrs = {k: v for k, v in rec.items()}
                system = None
            attrs["_table"] = table
            ts = rec.get("updated_at") or rec.get("indexed_at")
            obs = make_obs(file_id, rec["resource_id"], parse_ts(ts), True,
                           f"sql:{table}", attrs, system)
            if obs:
                observations.append(obs)
    return observations


# ---------------------------------------------------------------- JSON records

def parse_json(file_id, path, captured_at):
    data = json.loads(path.read_text(encoding="utf-8"))
    observations = []

    def add(resource_id, ts, present, source, attrs, system=None):
        obs = make_obs(file_id, resource_id, parse_ts(ts) if isinstance(ts, str) else ts,
                       present, source, attrs, system)
        if obs:
            observations.append(obs)

    if "service_exports" in data:                        # service_resource_export_*.json
        for system, sections in data["service_exports"].items():
            for section, items in sections.items():
                for item in items:
                    attrs = dict(item)
                    attrs["_section"] = section
                    if "status_code" in item:            # account lookups
                        present = item["status_code"] != 404
                        if item.get("profile"):
                            attrs.update(item["profile"])
                    elif "hits" in item:                 # search queries
                        docs = item["hits"].get("documents", []) if item["hits"] else []
                        present = bool(docs)
                        for d in docs:
                            attrs.update(d)
                    else:
                        present = True
                    add(item["resource_id"], item.get("observed_at"), present,
                        f"json:service_export:{section}", attrs, system)

    elif "snapshots" in data:                            # customer_data_snapshot_bundle_*.json
        for snap in data["snapshots"]:
            for rec in snap["records"]:
                if "result" in rec and "system" in rec:  # lookup-history style record
                    add(rec["resource_id"], rec.get("recorded_at"), present_from_result(rec["result"]),
                        f"json:bundle:{snap['collection']}", dict(rec), rec["system"])
                else:
                    attrs = dict(rec)
                    attrs["_collection"] = snap["collection"]
                    add(rec["resource_id"], snap["captured_at"], True,
                        f"json:bundle:{snap['collection']}", attrs, snap["data_store"])

    elif "records" in data:
        for rec in data["records"]:
            if "result" in rec and "system" in rec:      # manifest / ledger / connector lists
                add(rec["resource_id"], rec.get("recorded_at"), present_from_result(rec["result"]),
                    "json:record_list", dict(rec), rec["system"])
            else:                                        # flat collections (analytics, exports)
                attrs = dict(rec)
                attrs["_collection"] = data.get("collection")
                add(rec["resource_id"], data.get("captured_at") or captured_at, True,
                    f"json:{data.get('collection')}", attrs)
    return observations


# ---------------------------------------------------------------- plain text

def parse_txt(file_id, path):
    """Pipe-separated handover lines: ts | system | resource | record_type | result | subject."""
    observations = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("- ") or line.count("|") < 5:
            continue
        ts, system, resource, rtype, result, subject = [p.strip() for p in line[2:].split("|", 5)]
        attrs = {"record_type": rtype, "result": result, "subject_reference": subject}
        obs = make_obs(file_id, resource, parse_ts(ts), present_from_result(result),
                       "txt:handover", attrs, system)
        if obs:
            observations.append(obs)
    return observations


# ---------------------------------------------------------------- logs

def unquote(v):
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1].replace('\\"', '"')
    return v


def parse_log_line(line):
    """Return (timestamp, fields) for any of the log styles in the dataset."""
    m = TS_RE.match(line.strip())
    if not m:
        return None, {}
    ts, rest = m.group(1), m.group(2)
    fields = {}
    if rest.startswith("{"):                             # JSON lines
        try:
            fields = {k: str(v) for k, v in json.loads(rest).items()}
        except json.JSONDecodeError:
            pass
    elif rest.startswith("["):                           # [LEVEL] [service] [trace] event: k=v
        parts = re.match(r"\[(\w+)\]\s*\[([\w\-]+)\]\s*\[([\w\-]+)\]\s*(\w+):?\s*(.*)", rest)
        if parts:
            fields.update(level=parts.group(1), service=parts.group(2),
                          trace_id=parts.group(3), event=parts.group(4))
            rest = parts.group(5)
    elif " | " in rest:                                  # LEVEL | service | trace | event | k=v
        parts = [p.strip() for p in rest.split(" | ", 4)]
        if len(parts) == 5:
            fields.update(level=parts[0], service=parts[1], trace_id=parts[2], event=parts[3])
            rest = parts[4]
    elif rest.startswith("<"):                           # syslog: <pri>1 host service trace - - k=v
        parts = rest.split(" ", 6)
        if len(parts) >= 7:
            fields.update(service=parts[2], trace_id=parts[3])
            rest = parts[6]
    for k, v in KV_RE.findall(rest):
        fields.setdefault(k, unquote(v))
    if "message" in fields and "=" in fields["message"]:  # nested key=value inside message="..."
        for k, v in KV_RE.findall(fields.pop("message")):
            fields.setdefault(k, unquote(v))
    if "requestId" in fields:
        fields.setdefault("trace_id", fields["requestId"])
    return parse_ts(ts), fields


def log_presence(f):
    """Decide presence from one log line's fields, if the line says anything about it."""
    if "http_status" in f:
        return f["http_status"] != "404"
    if f.get("result") in {"hit", "miss"}:
        return f["result"] == "hit"
    if "result" in f:
        return present_from_result(f["result"])
    if "rows" in f:
        return f["rows"] != "0"
    return None


def parse_log(file_id, path):
    """Group lines by trace_id so search keys and results from different lines join up."""
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        ts, f = parse_log_line(raw)
        if ts:
            lines.append((ts, f))

    trace_keys = {}                                      # trace_id -> identifiers seen on any line
    for ts, f in lines:
        if f.get("trace_id"):
            trace_keys.setdefault(f["trace_id"], set()).update(extract_identifiers(f))

    merged = {}                                          # (trace or line, resource) -> observation
    for i, (ts, f) in enumerate(lines):
        rid = f.get("resource_id")
        if not rid or not system_for(rid):
            continue
        key = (f.get("trace_id") or f"line{i}", rid)
        obs = merged.get(key)
        if obs is None:
            obs = make_obs(file_id, rid, ts, None, "log", {})
            merged[key] = obs
        obs.attrs.update({k: v for k, v in f.items() if k not in obs.attrs})
        obs.observed_at = max(obs.observed_at, ts)
        p = log_presence(f)
        if p is not None:
            obs.present = p
        obs.identifiers |= extract_identifiers(f)
        if f.get("trace_id"):
            obs.identifiers |= trace_keys.get(f["trace_id"], set())
        if not f.get("event", "").endswith("_started"):
            obs.attrs.setdefault("_read_completed", True)
    for obs in merged.values():
        # A completed read or scan that returned fields, with no explicit
        # "not found" signal, means the record was there.
        if obs.present is None and obs.attrs.get("_read_completed"):
            obs.present = True
    return list(merged.values())


# ---------------------------------------------------------------- driver

TEXT_PARSERS = {".sql": "sql", ".json": "json", ".txt": "txt", ".log": "log"}


def parse_all(evidence_files, dataset_dir=DATASET_DIR):
    """Parse every text-based evidence file listed in the manifest."""
    observations, skipped = [], []
    for row in evidence_files:
        path = dataset_dir / row["file_path"]
        kind = TEXT_PARSERS.get(path.suffix)
        if kind == "sql":
            observations += parse_sql(row["file_id"], path)
        elif kind == "json":
            observations += parse_json(row["file_id"], path, row["captured_at"])
        elif kind == "txt":
            observations += parse_txt(row["file_id"], path)
        elif kind == "log":
            observations += parse_log(row["file_id"], path)
        else:
            skipped.append(row["file_id"])
    return observations, skipped


if __name__ == "__main__":
    from collections import Counter
    from loaders import load_all

    data = load_all()
    obs, skipped = parse_all(data["evidence_files"])
    print(f"observations: {len(obs)}   files skipped (image/pdf, handled by vision step): {len(skipped)}")
    print("\nby system:")
    for system, n in Counter(o.system_id for o in obs).most_common():
        print(f"  {system:24} {n}")
    print("\npresence:", dict(Counter(o.present for o in obs)))
    no_ids = [o for o in obs if not o.identifiers]
    print("observations with no identifiers:", len(no_ids))


# ---------------------------------------------------------------- activity stream

def parse_activity(activity):
    """Turn the few activity events that prove a state (not just claim one) into observations.

    Most activity rows are workflow claims ("deletion SUCCESS") and are ignored as proof.
    These are kept because they record an observed fact, cited by activity_id:
    - LINK_RESOURCE_IDENTITY: the data catalog tied a resource to an email and user_id
    - CHECK_DATA_FLOW: a connector status check found the flow still enabled
    - CHECK_RESTORE_ACCESS: a check found an archive can still be restored
    - SYNC_RECORD / RESTORE_BACKUP "repopulated": a process recreated a downstream record
    """
    observations = []
    for a in activity:
        rid, details = a["resource_id"], a["details"]
        if not system_for(rid):
            continue
        attrs = {"event_type": a["event_type"], "details": details, "_source": "activity"}
        present = None
        if a["event_type"] == "LINK_RESOURCE_IDENTITY":
            attrs["_link_only"] = True
        elif a["event_type"] == "CHECK_DATA_FLOW" and "remains enabled" in details:
            present, attrs["enabled"] = True, "true"
        elif a["event_type"] == "CHECK_RESTORE_ACCESS" and "can still be restored" in details:
            present, attrs["restore_allowed"] = True, "true"
        elif "repopulated" in details:
            present = True
        else:
            continue
        observations.append(Observation(a["activity_id"], system_for(rid), rid, a["event_time"],
                                        present, "activity", attrs, extract_identifiers(details)))
    return observations
