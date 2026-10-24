"""Step 0 of every case: does the request actually authorize deletion?

Two classifiers:
- rule_based(): a transparent multilingual keyword baseline. Always available,
  costs nothing, and is the fallback when no LLM answers.
- The LLM classifier lives in llm.py and is preferred when a key is configured,
  because requests are free text in many languages and can mention deletion
  while asking for something else ("please don't delete anything").
"""

import json
import re

# Phrases that ask for deletion, across the languages seen in the data.
DELETION = re.compile(
    r"delet|erase|eras(e|ure)|remov|close (my|this|the) account|clos(e|ing) (my|this)|"
    r"suppr(im|ess)|profile is gone|can(not|'t) identify me|"
    r"effac|elimin|borr|lösch|loesch|apag|exclu|verwijder|usu[nń]|"
    r"削除|消去|退会|탈퇴|삭제|删除|注销",
    re.IGNORECASE,
)
# Phrases that keep the account or ask for a different right (access, correction, opt-out).
NON_DELETION = re.compile(
    r"don'?t delete|do not (delete|close|remove)|without removing|keep the account|"
    r"leave (the|my) (account|profile)|continue using|still (want|use)|"
    r"send me a copy|copy of (last|the information|my data|your)|download my|portable format|correct it|misspell|out of date|replace it|"
    r"change the email|reset(ting)? the login|marketing lists only|unsubscribe|"
    r"not asking for (deletion|erasure)|not a request to (erase|delete|remove)|promotional emails|"
    r"information request|what personal information you hold",
    re.IGNORECASE,
)


def rule_based(text):
    """Return True if the request authorizes deletion."""
    if NON_DELETION.search(text):
        return False
    return bool(DELETION.search(text))


# ------------------------------------------------------------------ LLM classifier

INTENT_PROMPT = """You are the intake classifier for a privacy team. For each customer message, decide
whether the customer is asking the company to DELETE or ERASE their account or personal data
(including asking to finish, verify, or complete a deletion that was already started).

Answer is_deletion = false for every other request type, even if the message mentions deletion:
access or copy of data, data export or download, correcting or updating details, changing the
email, login help, marketing or newsletter opt-out, or any message that says to keep the account.

Messages may be in any language. Judge the request itself, not the tone.

Return JSON only, in exactly this shape:
{"results": [{"case_id": "<id>", "is_deletion": true|false, "request_type": "<deletion|access|export|correction|marketing_opt_out|account_support|other>"}]}

Messages:
"""

BATCH_SIZE = 25


def classify_all(requests, use_llm=True, log=print):
    """Classify every request. Returns {case_id: (is_deletion, source)}.

    Requests are sent to the LLM in batches of 25 to stay inside free-tier daily
    request limits. Any batch the LLM cannot answer falls back to rule_based().
    """
    from llm import LLMUnavailable, ask_llm

    decisions = {}
    ordered = sorted(requests, key=lambda r: int(r["case_id"].split("_")[1]))
    for start in range(0, len(ordered), BATCH_SIZE):
        batch = ordered[start:start + BATCH_SIZE]
        answers = {}
        if use_llm:
            lines = [json.dumps({"case_id": r["case_id"], "message": r["request_text"]}, ensure_ascii=False)
                     for r in batch]
            try:
                out = ask_llm("intent_classification", INTENT_PROMPT + "\n".join(lines))
                answers = {a["case_id"]: bool(a["is_deletion"]) for a in out.get("results", [])}
            except LLMUnavailable as e:
                log(f"  intent batch {start // BATCH_SIZE + 1}: LLM unavailable, using rules ({str(e)[:120]})")
        for r in batch:
            if r["case_id"] in answers:
                decisions[r["case_id"]] = (answers[r["case_id"]], "llm")
            else:
                decisions[r["case_id"]] = (rule_based(r["request_text"]), "rules")
    return decisions
