# Token Usage and Cost Report

Generated 2026-10-24T20:26:03+00:00 by `python3 code/main.py`, covering the full-dataset run that wrote `output.csv` (518 requests).

Model calls happen only at the unstructured edges: batched intent classification (25 requests per call) and one vision extraction per screenshot or PDF. Linking, policy decisions and step planning are deterministic Python and use no tokens.

## Per model

| Provider | Model | Tasks | Calls | Live / cached | Input tokens | Output tokens | Total tokens | Est. cost (USD) |
|---|---|---|---|---|---|---|---|---|
| google-gemini | gemini-3.5-flash-lite | intent_classification | 22 | 0 / 22 | 33,864 | 20,241 | 54,105 | 0.00 (free tier) |
| google-gemini | gemini-3.8-flash | vision_extraction | 15 | 0 / 15 | 21,818 | 3,894 | 25,712 | 0.00 (free tier) |

## Overall

- Model calls: 37
- Input tokens: 55,682
- Output tokens: 24,135
- Total tokens: 79,817
- Average tokens per request: 154.1
- Estimated total cost: $0.0000
- Estimated cost per request: $0.000000

Costs are $0 because both providers were used on their free tiers. Set the `*_USD_PER_M` environment variables to price the same token counts at paid-tier rates.
