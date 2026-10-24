"""Step 3b: read screenshots and PDFs with a vision-capable LLM.

Images and PDFs are ordinary business artifacts (admin consoles, handover
reports, attachments). No parser can read them, so a multimodal model extracts
the records they show into structured JSON, and we convert that JSON into the
same Observation shape as every other evidence file. From that point on the
pipeline treats screenshot facts exactly like database facts.

The model only extracts what is visible. It never decides deletion status;
the policy engine does that.
"""

import json
import re

from evidence import Observation, extract_identifiers, system_for
from identities import norm_email, norm_phone
from llm import LLMUnavailable, ask_llm
from loaders import DATASET_DIR

VISION_PROMPT = """You are reading an internal business artifact (an admin-console screenshot or a PDF
document) from an online shop called Northstar Shop. Extract every data record it shows.

A record is anything with a resource identifier such as account_5762, support_9538, search_428,
analytics_7438, export_1258, backup_9281, data_flow_1121, audit_record_003, tombstone_12,
legal_hold_8, marketing_001, cache_44, or a customer reference such as user_11.

For each record return:
- resource_id: exactly as printed (e.g. "support_9538"); null if only a user reference is shown
- emails, phones, names: personal identifiers printed in or next to that record
- user_refs: customer references like "user_11" tied to that record
- status: what the artifact says about the record, one of
  "present", "deleted", "not_found", "redacted", "anonymized", "unknown"
- enabled: true/false if the record is a sync, connector or integration and its state is shown, else null
- restore_allowed: true/false if the record is a backup and restore permission is shown, else null
- available: true/false if the record is an export or download and availability is shown, else null

Copy identifiers character by character. Do not correct spelling, because look-alike
addresses can belong to different people. Do not invent anything that is not visible.

Return JSON only:
{"records": [{"resource_id": "...", "emails": [], "phones": [], "names": [], "user_refs": [],
              "status": "...", "enabled": null, "restore_allowed": null, "available": null}]}
"""

MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".pdf": "application/pdf"}
RESOURCE_RE = re.compile(r"^[a-z_]+_\d+$")


def to_observations(file_id, captured_at, extracted, artifact=""):
    observations = []
    for rec in extracted.get("records", []):
        rid = (rec.get("resource_id") or "").strip()
        if not RESOURCE_RE.match(rid) or not system_for(rid):
            continue
        status = str(rec.get("status") or "unknown").lower()
        present = False if status in {"deleted", "not_found"} else (None if status == "unknown" else True)
        attrs = {"_source": "vision", "status": status, "_artifact": artifact}
        for flag in ("enabled", "restore_allowed", "available"):
            if rec.get(flag) is not None:
                attrs[flag] = str(bool(rec[flag])).lower()
        if status == "anonymized":
            attrs["result"] = "ANONYMIZED"
        elif status == "redacted":
            attrs["result"] = "REDACTED"
        names = [n for n in rec.get("names") or [] if n]
        if names:
            attrs["name"] = names[0]
        identifiers = {norm_email(e) for e in rec.get("emails") or [] if e}
        identifiers |= {u for u in rec.get("user_refs") or [] if re.match(r"^user_\d+$", u or "")}
        identifiers |= {"phone:" + norm_phone(p) for p in rec.get("phones") or [] if p and norm_phone(p)}
        identifiers |= extract_identifiers(attrs)
        observations.append(Observation(file_id, system_for(rid), rid, captured_at, present,
                                        "vision", attrs, identifiers))
    return observations


def parse_media(evidence_files, dataset_dir=DATASET_DIR, log=print):
    """Run vision extraction over every image and PDF in the manifest (cached per file)."""
    observations, failed = [], []
    for row in evidence_files:
        path = dataset_dir / row["file_path"]
        mime = MIME.get(path.suffix.lower())
        if not mime:
            continue
        try:
            extracted = ask_llm("vision_extraction", VISION_PROMPT,
                                attachments=[(path.name, path.read_bytes(), mime)], vision=True)
            observations += to_observations(row["file_id"], row["captured_at"], extracted, path.name)
        except LLMUnavailable as e:
            failed.append(row["file_id"])
            log(f"  vision {row['file_id']} ({path.name}): skipped, {str(e)[:120]}")
    return observations, failed
