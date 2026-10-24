# Token Usage and Cost Report

Generated 2026-10-24T19:49:09+00:00 by `python3 code/main.py`, covering the full-dataset run that wrote `output.csv` (518 requests).

Model calls happen only at the unstructured edges: batched intent classification (25 requests per call) and one vision extraction per screenshot or PDF. Linking, policy decisions and step planning are deterministic Python and use no tokens.

## Per model

| Provider | Model | Tasks | Calls | Live / cached | Input tokens | Output tokens | Total tokens | Est. cost (USD) |
|---|---|---|---|---|---|---|---|---|

## Overall

- Model calls: 0
- Input tokens: 0
- Output tokens: 0
- Total tokens: 0
- Average tokens per request: 0.0
- Estimated total cost: $0.0000
- Estimated cost per request: $0.000000

Costs are $0 because both providers were used on their free tiers. Set the `*_USD_PER_M` environment variables to price the same token counts at paid-tier rates.

No model calls were made in this run (deterministic mode, `--no-llm`).
