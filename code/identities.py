"""Step 2: identity resolution.

Turn a request email into one person (user_id) and that person's full key set:
every email, phone, and email hash they have ever had, plus their name.
Later steps search every system for ANY of these keys, so records copied
before an email or phone change are still found.

Sources:
- user_identities.csv: current snapshot (SCD Type 1, one row per user)
- user_audit_log.csv: history of changes (old_value -> new_value)
Together they give an SCD Type 2 view of each user.
"""

import hashlib
import re
from collections import defaultdict


def norm_email(value):
    """Emails are case-insensitive in practice; evidence mixes upper and lower case."""
    return value.strip().lower() if value else None


def norm_phone(value):
    """Keep digits only, so '+1 (312) 555-0101' and '+13125550101' match."""
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    return digits or None


def email_hash(email):
    """The dataset's email_hash is SHA-256 of the lowercase email (verified on samples)."""
    return hashlib.sha256(norm_email(email).encode("utf-8")).hexdigest()


class IdentityIndex:
    """Lookup tables built once from the identity snapshot and its history."""

    def __init__(self, identities, audit_log):
        self.users = {}                      # user_id -> key set
        self.email_to_user = {}              # normalized email -> user_id
        keys = defaultdict(lambda: {"emails": set(), "phones": set(), "hashes": set(), "names": set()})

        # Current snapshot.
        for row in identities:
            uid = row["user_id"]
            keys[uid]["emails"].add(norm_email(row["email"]))
            keys[uid]["phones"].add(norm_phone(row["phone"]))
            keys[uid]["hashes"].add(row["email_hash"].lower())
            keys[uid]["names"].add(row["name"].strip())

        # History: both old and new values belong to the same person.
        for row in audit_log:
            uid, field = row["user_id"], row["field_name"]
            for value in (row["old_value"], row["new_value"]):
                if not value:
                    continue
                if field == "email":
                    keys[uid]["emails"].add(norm_email(value))
                elif field == "phone":
                    keys[uid]["phones"].add(norm_phone(value))
                elif field == "email_hash":
                    keys[uid]["hashes"].add(value.lower())

        for uid, k in keys.items():
            # Hash every known email too, in case a historical hash was never logged.
            k["hashes"] |= {email_hash(e) for e in k["emails"]}
            for part in ("emails", "phones", "hashes", "names"):
                k[part].discard(None)
            k["user_id"] = uid
            self.users[uid] = k
            for e in k["emails"]:
                if e in self.email_to_user and self.email_to_user[e] != uid:
                    raise ValueError(f"Email {e} maps to two users; resolution would be ambiguous")
                self.email_to_user[e] = uid

    def resolve(self, request_email):
        """Return the full key set for the person behind a request email, or None."""
        uid = self.email_to_user.get(norm_email(request_email))
        return self.users.get(uid) if uid else None


if __name__ == "__main__":
    from loaders import load_all

    data = load_all()
    index = IdentityIndex(data["identities"], data["audit_log"])

    all_requests = data["requests"] + data["sample_requests"]
    unresolved = [r["case_id"] for r in all_requests if index.resolve(r["email"]) is None]
    print(f"users indexed:      {len(index.users)}")
    print(f"emails indexed:     {len(index.email_to_user)}")
    print(f"requests resolved:  {len(all_requests) - len(unresolved)} / {len(all_requests)}")

    example = index.resolve("noah.f438085@outlook.com")   # case_29, an OLD email
    print("\ncase_29 example:")
    for part in ("user_id", "emails", "phones", "hashes"):
        value = example[part]
        if part == "hashes":
            value = sorted(h[:12] + "..." for h in value)
        print(f"  {part:8} {sorted(value) if isinstance(value, set) else value}")
