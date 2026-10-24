"""LLM access layer: one function, two providers, a disk cache, and usage logging.

Why this shape:
- Provider failover. Free tiers run out. ask_llm() tries Gemini first, then Groq.
  The caller never knows or cares which one answered.
- Disk cache. Every successful answer is stored under a hash of
  (task, model input). Reruns and debugging cost zero quota, and the final run is
  reproducible.
- Usage log. Every real API call records provider, model, and token counts,
  which evaluation/usage_report.py turns into the required usage_report.md.

Only the Python standard library is used (urllib), so there is nothing to install.
Keys come from environment variables or a local .env file that git ignores.
"""

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
REPO_ROOT = CODE_DIR.parent
CACHE_DIR = CODE_DIR / "cache"
CACHE_FILE = CACHE_DIR / "llm_cache.json"
USAGE_FILE = CACHE_DIR / "usage_log.jsonl"

DEFAULT_GEMINI_MODEL = "gemini-flash-lite-latest"
DEFAULT_GEMINI_VISION_MODEL = "gemini-flash-latest"
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"


def load_dotenv(path=REPO_ROOT / ".env"):
    """Minimal .env reader: KEY=value lines, no export, no quotes needed."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()


class LLMUnavailable(Exception):
    """Raised when no provider could answer."""


# ------------------------------------------------------------------ cache

def _load_cache():
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    return {}


_CACHE = _load_cache()

# Every answer used during this process run (live or from cache), for the usage report.
RUN_USAGE = []


def _save_cache():
    CACHE_DIR.mkdir(exist_ok=True)
    CACHE_FILE.write_text(json.dumps(_CACHE, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _cache_key(task, prompt, attachments):
    h = hashlib.sha256()
    h.update(task.encode())
    h.update(prompt.encode("utf-8"))
    for name, data, _ in attachments or []:
        h.update(name.encode())
        h.update(hashlib.sha256(data).digest())
    return h.hexdigest()


def _log_usage(task, provider, model, input_tokens, output_tokens, seconds):
    CACHE_DIR.mkdir(exist_ok=True)
    with open(USAGE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "task": task, "provider": provider, "model": model,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "seconds": round(seconds, 2),
        }) + "\n")


# ------------------------------------------------------------------ providers

def _post_json(url, payload, headers, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _gemini(prompt, attachments, vision):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise LLMUnavailable("GEMINI_API_KEY not set")
    model = os.environ.get("GEMINI_VISION_MODEL" if vision else "GEMINI_MODEL",
                           DEFAULT_GEMINI_VISION_MODEL if vision else DEFAULT_GEMINI_MODEL)
    parts = [{"text": prompt}]
    for _, data, mime in attachments or []:
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}})
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    out = _post_json(url, payload, {"x-goog-api-key": key})
    text = "".join(p.get("text", "") for p in out["candidates"][0]["content"]["parts"])
    usage = out.get("usageMetadata", {})
    return text, "google-gemini", model, usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0)


def _groq(prompt, attachments, vision):
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise LLMUnavailable("GROQ_API_KEY not set")
    model = os.environ.get("GROQ_VISION_MODEL" if vision else "GROQ_MODEL", "" if vision else DEFAULT_GROQ_MODEL)
    if vision and not model:
        raise LLMUnavailable("GROQ_VISION_MODEL not set")
    content = [{"type": "text", "text": prompt}]
    for _, data, mime in attachments or []:
        if not mime.startswith("image/"):
            raise LLMUnavailable("Groq fallback only accepts images")
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}})
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": content if attachments else prompt}],
    }
    out = _post_json("https://api.groq.com/openai/v1/chat/completions", payload,
                     {"Authorization": f"Bearer {key}", "User-Agent": "ghost-data-audit/1.0"})
    usage = out.get("usage", {})
    return (out["choices"][0]["message"]["content"], "groq", model,
            usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))


PROVIDERS = [("gemini", _gemini), ("groq", _groq)]


def ask_llm(task, prompt, attachments=None, vision=False, retries=2):
    """Return parsed JSON from the first provider that answers. Cached on disk.

    attachments: list of (name, bytes, mime_type) for images or PDFs.
    """
    key = _cache_key(task, prompt, attachments)
    if key in _CACHE:
        entry = _CACHE[key]
        RUN_USAGE.append({"task": task, "provider": entry["provider"], "model": entry["model"],
                          "input_tokens": entry.get("input_tokens", 0),
                          "output_tokens": entry.get("output_tokens", 0), "cached": True})
        return entry["response"]

    errors = []
    for name, call in PROVIDERS:
        for attempt in range(retries + 1):
            try:
                start = time.time()
                text, provider, model, tin, tout = call(prompt, attachments, vision)
                parsed = json.loads(text)
                _log_usage(task, provider, model, tin, tout, time.time() - start)
                _CACHE[key] = {"task": task, "provider": provider, "model": model, "response": parsed,
                               "input_tokens": tin, "output_tokens": tout}
                _save_cache()
                RUN_USAGE.append({"task": task, "provider": provider, "model": model,
                                  "input_tokens": tin, "output_tokens": tout, "cached": False})
                return parsed
            except LLMUnavailable as e:
                errors.append(f"{name}: {e}")
                break
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:300]
                errors.append(f"{name}: HTTP {e.code} {body}")
                if e.code == 429 and attempt < retries:   # rate limited: back off, then retry
                    time.sleep(20 * (attempt + 1))
                    continue
                break
            except (urllib.error.URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError) as e:
                errors.append(f"{name}: {type(e).__name__} {e}")
                if attempt < retries:
                    time.sleep(3)
                    continue
                break
    raise LLMUnavailable("; ".join(errors))


def list_models():
    """Print the model IDs each configured key can use, to pick GEMINI_MODEL / GROQ_MODEL."""
    if os.environ.get("GEMINI_API_KEY"):
        req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                                     headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]})
        with urllib.request.urlopen(req, timeout=60) as r:
            models = json.loads(r.read())["models"]
        print("Gemini:", ", ".join(m["name"].split("/")[-1] for m in models
                                   if "generateContent" in m.get("supportedGenerationMethods", [])))
    if os.environ.get("GROQ_API_KEY"):
        req = urllib.request.Request("https://api.groq.com/openai/v1/models",
                                     headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}",
                                              "User-Agent": "ghost-data-audit/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            print("Groq:  ", ", ".join(m["id"] for m in json.loads(r.read())["data"]))


if __name__ == "__main__":
    import sys
    if "--list-models" in sys.argv:
        list_models()
    else:
        print(ask_llm("smoke_test", 'Reply with the JSON object {"ok": true}.'))
