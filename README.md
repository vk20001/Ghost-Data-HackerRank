# Ghost Data Still Exists: Deletion Audit Agent

An audit agent that checks whether a customer's personal data is really gone after a
deletion request, across every system that holds a copy, and writes the ordered
remediation or verification plan. Built for HackerRank Orchestrate (October 2026).
The challenge contract is in [`problem_statement.md`](./problem_statement.md).

## Quick start

Requires Python 3.10+. No third-party packages: the code uses only the standard library.

```bash
# 1. API keys (free tiers are enough). Create .env in the repo root (it is git-ignored):
#    GEMINI_API_KEY=...
#    GROQ_API_KEY=...          # optional backup provider

# 2. Full run: LLM intent + vision + audit -> output.csv at the repo root
python3 code/main.py

# 3. Deterministic run without any API (keyword intent, no screenshot reading)
python3 code/main.py --no-llm

# 4. Evaluation on the 32 labeled samples (replays cached model answers, costs no tokens)
python3 code/evaluation/main.py
python3 code/evaluation/custom_cases.py
python3 code/evaluation/report.py      # regenerates code/evaluation/evaluation_report.md

# Helpers
python3 code/llm.py --list-models      # model IDs your keys can use
python3 code/llm.py                    # one-call smoke test
```

Optional `.env` settings: `GEMINI_MODEL` (default `gemini-3.5-flash-lite`),
`GEMINI_VISION_MODEL` (default `gemini-3.8-flash`), `GROQ_MODEL` (default
`openai/gpt-oss-120b`), `GROQ_VISION_MODEL` (unset: no Groq vision fallback),
and `*_USD_PER_M` prices to estimate paid-tier cost in the usage report.

## Architecture

```
deletion_requests.csv
        |
        v
 [1] Intent ............ LLM (batched, 25 per call) -> keyword rules as fallback
        |                 NOT_APPLICABLE stops here with []
        v
 [2] Identity .......... current snapshot + audit-log history -> every email, phone, hash, user_id
        |
 [3] Evidence .......... 79 text files parsed deterministically (SQL in SQLite, JSON, logs joined
        |                 on trace_id, handover text) + 15 images/PDFs read by a vision model
        |                 + activity events that prove state -> one Observation table
        v
 [4] Linking ........... identifier match; deleted accounts via the deletion job
        |                 (time window + result agrees with reported status + one account per case)
        v
 [5] Policy ............ category from record content, treatment from deletion_rules.csv,
        |                 state reconstructed at audit_at from the latest earlier evidence
        v
 [6] Planner ........... findings -> remediation -> verification, in the contract's order;
                          disable flows and block restores before deleting
        |
        v
   output.csv (validated: 518 rows, step numbering, empty evidence on future steps)
```

**Design principle: models at the unstructured edges, deterministic logic in the core.**
LLMs do two jobs only: deciding whether free-text, multilingual requests authorize
deletion, and turning screenshots and PDFs into structured records. Every compliance
decision (what belongs to the person, what state it was in at `audit_at`, what the policy
requires, what order to fix it in) is plain Python that reads `deletion_rules.csv`, so the
same input always gives the same answer and every step can be traced to evidence.

| Module | Responsibility |
|---|---|
| `code/loaders.py` | Read the dataset CSVs, parse timestamps |
| `code/identities.py` | SCD2-style identity view: all historical keys per user |
| `code/evidence.py` | Parsers for every text evidence format into `Observation` rows |
| `code/vision.py` | Vision-model extraction for images and PDFs into the same rows |
| `code/linking.py` | Which records belong to which request |
| `code/policy.py` | Category, required treatment, state at `audit_at` |
| `code/planner.py` | Ordered `steps_json` |
| `code/intent.py` | Intent classification (LLM batched + rules fallback) |
| `code/llm.py` | Provider failover (Gemini -> Groq), disk cache, token logging |
| `code/pipeline.py` | Wires the steps together per request |
| `code/main.py` | Entry point, output validation, usage report |
| `code/evaluation/` | Scoring harness, custom cases, evaluation and usage reports |

## Key decisions

- **Claims vs proof.** Workflow statuses, job results and data-catalog links are claims.
  State comes from observed records (lookups, snapshots, screenshots). A catalog link is
  dropped when the observed record shows only someone else's identifiers.
- **Point in time.** A record's state is the latest observation at or before `audit_at`
  whose file was captured by then; later evidence never changes the answer.
- **Linking without identifiers.** After deletion, an account lookup only says "404
  account_3136". The deletion job in `system_activity.csv` is the remaining link; it must
  fall in a short window before the reported status, and its result must agree with that
  status. Accounts claimed by one case are removed from the others' candidates.
- **Cost and reliability.** Batching, a disk cache, and provider failover keep the full run
  at about 37 model calls on free tiers; reruns cost nothing. See
  `code/evaluation/usage_report.md`.

## Results

On the 32 labeled samples: status accuracy 100%, step F1 0.966, exact sequence match
71.9%. Details, ablation, custom cases and remaining failures are in
`code/evaluation/evaluation_report.md`.

## Repository notes

- `code/cache/llm_cache.json` holds the model answers used for `output.csv`, so the
  evaluation can be reproduced without API keys.
- `log.txt` is the development transcript required by the challenge.
- Never commit `.env`; keys are read from the environment only.
