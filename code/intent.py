"""Step 0 of every case: does the request actually authorize deletion?

Two classifiers:
- rule_based(): a transparent multilingual keyword baseline. Always available,
  costs nothing, and is the fallback when no LLM answers.
- The LLM classifier lives in llm.py and is preferred when a key is configured,
  because requests are free text in many languages and can mention deletion
  while asking for something else ("please don't delete anything").
"""

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
