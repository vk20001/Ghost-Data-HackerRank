"""Write evaluation/usage_report.md from the model calls used in the latest run.

Counts every LLM answer that produced output.csv, whether it was fetched live in
this run or reused from the cache (a cached answer still cost tokens once).
Cost: free-tier usage is billed at $0. To estimate paid-tier cost, set per-million
token prices in the environment, e.g. GEMINI_INPUT_USD_PER_M / GEMINI_OUTPUT_USD_PER_M
and GROQ_INPUT_USD_PER_M / GROQ_OUTPUT_USD_PER_M, using the provider's current price page.
"""

import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPORT = Path(__file__).resolve().parent / "usage_report.md"
PRICE_ENV = {"google-gemini": "GEMINI", "groq": "GROQ"}


def price(provider, kind):
    v = os.environ.get(f"{PRICE_ENV.get(provider, provider.upper())}_{kind}_USD_PER_M")
    return float(v) if v else None


def write_report(n_requests):
    from llm import RUN_USAGE

    per_model = defaultdict(lambda: {"calls": 0, "live": 0, "cached": 0, "in": 0, "out": 0, "tasks": set()})
    for u in RUN_USAGE:
        m = per_model[(u["provider"], u["model"])]
        m["calls"] += 1
        m["live" if not u["cached"] else "cached"] += 1
        m["in"] += u["input_tokens"]
        m["out"] += u["output_tokens"]
        m["tasks"].add(u["task"])

    lines = [
        "# Token Usage and Cost Report",
        "",
        f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} by `python3 code/main.py`, "
        f"covering the full-dataset run that wrote `output.csv` ({n_requests} requests).",
        "",
        "Model calls happen only at the unstructured edges: batched intent classification "
        "(25 requests per call) and one vision extraction per screenshot or PDF. Linking, policy "
        "decisions and step planning are deterministic Python and use no tokens.",
        "",
        "## Per model",
        "",
        "| Provider | Model | Tasks | Calls | Live / cached | Input tokens | Output tokens | Total tokens | Est. cost (USD) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    tot = {"calls": 0, "in": 0, "out": 0, "cost": 0.0, "priced": True}
    for (provider, model), m in sorted(per_model.items()):
        pin, pout = price(provider, "INPUT"), price(provider, "OUTPUT")
        if pin is not None and pout is not None:
            cost = (m["in"] * pin + m["out"] * pout) / 1e6
            cost_s = f"{cost:.4f}"
        else:
            cost, cost_s = 0.0, "0.00 (free tier)"
        tot["calls"] += m["calls"]
        tot["in"] += m["in"]
        tot["out"] += m["out"]
        tot["cost"] += cost
        lines.append(f"| {provider} | {model} | {', '.join(sorted(m['tasks']))} | {m['calls']} | "
                     f"{m['live']} / {m['cached']} | {m['in']:,} | {m['out']:,} | {m['in'] + m['out']:,} | {cost_s} |")
    total_tokens = tot["in"] + tot["out"]
    lines += [
        "",
        "## Overall",
        "",
        f"- Model calls: {tot['calls']}",
        f"- Input tokens: {tot['in']:,}",
        f"- Output tokens: {tot['out']:,}",
        f"- Total tokens: {total_tokens:,}",
        f"- Average tokens per request: {total_tokens / n_requests:,.1f}" if n_requests else "",
        f"- Estimated total cost: ${tot['cost']:.4f}",
        f"- Estimated cost per request: ${tot['cost'] / n_requests:.6f}" if n_requests else "",
        "",
        "Costs are $0 because both providers were used on their free tiers. Set the "
        "`*_USD_PER_M` environment variables to price the same token counts at paid-tier rates.",
    ]
    if not RUN_USAGE:
        lines += ["", "No model calls were made in this run (deterministic mode, `--no-llm`)."]
    REPORT.write_text("\n".join(l for l in lines if l is not None) + "\n", encoding="utf-8")
