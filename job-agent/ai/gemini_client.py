"""Single entry point for every LLM call in the project.

Wraps `google-generativeai` so the rest of the codebase doesn't have to know
about the SDK. Reads `GEMINI_API_KEY` from `.env`.

Free tier: https://aistudio.google.com/app/apikey
Default model: gemini-2.0-flash

Usage:
    from ai.gemini_client import ask_gemini

    text   = ask_gemini("Tell me a haiku about resumes.")
    parsed = ask_gemini("Return JSON: {\"ok\": true}", expect_json=True)
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import warnings
import yaml
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# The `google.generativeai` SDK prints a FutureWarning on import announcing
# its long-term deprecation in favor of `google.genai`. Both still work with
# `gemini-2.0-flash`; suppress the noise so CLI output stays clean.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", FutureWarning)
    try:
        import google.generativeai as genai
    except ImportError:  # pragma: no cover - raised at first call
        genai = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_config() -> dict[str, Any]:
    """Loads the main config.yaml file."""
    config_path = PROJECT_ROOT / "config" / "config.yaml"
    if not config_path.exists():
        return {}
    with config_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


_config = _load_config()
_ai_config = _config.get("ai", {})
_provider = _ai_config.get("provider")

_FALLBACK_GEMINI_MODEL = "gemini-2.0-flash"


def _looks_like_gemini_model(model_id: str) -> bool:
    mid = (model_id or "").strip().lower()
    if not mid:
        return False
    return mid.startswith("gemini-") or mid.startswith("models/gemini-")


def _resolve_default_model() -> str:
    """Only Google Gemini model ids work with this client; never pass through
    OpenAI/Groq-style ids from config (they would fail at API call time)."""
    raw = str(_ai_config.get("model") or "").strip()
    prov = str(_provider or "").strip().lower()
    ok_provider = prov in ("", "gemini", "google")
    ok_model = _looks_like_gemini_model(raw)

    if ok_provider and ok_model:
        return raw

    if _provider and not ok_provider:
        print(
            f"Warning: config.yaml specifies AI provider '{_provider}', but only "
            "'gemini' is supported by the current client (gemini_client.py).",
            file=sys.stderr,
        )
    if raw and not ok_model:
        print(
            f"Warning: config.ai.model {raw!r} is not a Gemini model id; "
            f"using {_FALLBACK_GEMINI_MODEL!r} instead.",
            file=sys.stderr,
        )
    return _FALLBACK_GEMINI_MODEL


DEFAULT_MODEL = _resolve_default_model()
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 4096

# Retries for 429 / quota / resource-exhausted (free tier often asks to wait).
_MAX_RETRIES = max(1, min(12, int(_ai_config.get("max_retries", 6))))
_WEB_PARSE_RETRIES = max(1, min(6, int(_ai_config.get("web_parse_max_retries", 2))))


def resolve_parse_model() -> str:
    """Model for resume parsing — often flash-lite (separate free-tier quota)."""
    raw = str(_ai_config.get("parse_model") or "").strip()
    if raw and _looks_like_gemini_model(raw):
        return raw
    # Prefer flash-lite when parse_model is unset (separate free-tier quota).
    lite = "gemini-2.0-flash-lite"
    if _looks_like_gemini_model(lite):
        return lite
    fallback = _resolve_fallback_model()
    if fallback:
        return fallback
    return DEFAULT_MODEL


def web_parse_max_retries() -> int:
    """Fewer retries for interactive web uploads (fail fast with a clear error)."""
    return _WEB_PARSE_RETRIES


def web_ui_max_retries() -> int:
    """Fewer retries for interactive web AI actions (tailor, cover letter, etc.)."""
    raw = _ai_config.get("web_ui_max_retries")
    if raw is not None:
        return max(1, min(6, int(raw)))
    return _WEB_PARSE_RETRIES


def format_gemini_error(exc: Exception) -> str:
    """User-facing message for Gemini / parse failures in the web UI."""
    msg = str(exc)
    low = msg.lower()
    if "not set" in low and ("gemini_api_key" in low or "api_key" in low):
        return (
            "GEMINI_API_KEY is missing. Add it to your `.env` file "
            "(free key: https://aistudio.google.com/app/apikey)."
        )
    if (
        "429" in msg
        or "resource exhausted" in low
        or ("quota" in low and "exceed" in low)
        or "rate limit" in low
    ):
        return (
            "Gemini API quota exceeded (rate limit). Please wait a few minutes before trying again. "
            "If using the free tier, ensure a valid fallback model (like gemini-2.0-flash-lite) is set in config.yaml."
        )
    if _is_model_not_found(exc):
        return (
            "Gemini model not found. Check `ai.model` and `ai.parse_model` "
            f"in config.yaml. Details: {msg}"
        )
    if "google-generativeai is not installed" in low:
        return "Missing package — run: pip install google-generativeai"
    if "no extractable text" in low:
        return (
            "Could not read text from this PDF. Use a text-based PDF "
            "(not a scanned image-only file)."
        )
    return msg


def _resolve_fallback_model() -> str | None:
    """Optional second model from config (separate quota bucket in practice)."""
    raw = str(_ai_config.get("fallback_model") or "").strip()
    if not raw or not _looks_like_gemini_model(raw):
        return None
    if raw == DEFAULT_MODEL:
        return None
    return raw


def _is_retryable_quota(exc: Exception) -> bool:
    s = str(exc).lower()
    if "limit: 0" in s or "daily" in s:
        return False
    return (
        "429" in str(exc)
        or "resource exhausted" in s
        or ("quota" in s and "exceed" in s)
        or "rate limit" in s
    )


def _is_model_not_found(exc: Exception) -> bool:
    s = str(exc).lower()
    return "404" in str(exc) or ("not found" in s and "model" in s)


def _retry_delay_seconds(exc: Exception, attempt: int) -> float:
    """Prefer server-suggested delay; else exponential backoff with jitter."""
    text = str(exc)
    m = re.search(r"retry in ([\d.]+)\s*s", text, re.IGNORECASE)
    if m:
        return min(float(m.group(1)) + 0.5, 120.0)
    m2 = re.search(r"seconds:\s*(\d+)", text)
    if m2:
        return min(float(m2.group(1)) + 0.5, 120.0)
    return min(2.0**attempt + random.uniform(0.0, 0.75), 90.0)


_configured = False


def _ensure_configured() -> None:
    """Lazy-init genai with the API key from `.env`. Safe to call repeatedly."""
    global _configured
    if _configured:
        return
    if genai is None:
        raise RuntimeError(
            "google-generativeai is not installed. "
            "Run: pip install google-generativeai"
        )
    load_dotenv(PROJECT_ROOT / ".env")
    api_key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    if not api_key or api_key == "your-gemini-api-key-here":
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Add it to your .env file.\n"
            "Get a free key at https://aistudio.google.com/app/apikey"
        )
    genai.configure(api_key=api_key)
    _configured = True


# ---------------------------------------------------------------------------
# JSON handling (tolerant of fences / surrounding prose)
# ---------------------------------------------------------------------------
def _strip_fences(raw: str) -> str:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned


def _safe_json_parse(raw: str) -> Any:
    cleaned = _strip_fences(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    match = re.search(r"[\{\[][\s\S]*[\}\]]", raw)
    if not match:
        raise ValueError("No JSON object/array found in model response.")
    return json.loads(match.group(0))


def _extract_text(response: Any) -> str:
    """Pull text out of a Gemini response, handling both `.text` and
    `.candidates[].content.parts[].text` shapes."""
    text: str = ""
    try:
        text = (response.text or "").strip()
    except Exception:
        text = ""
    if text:
        return text
    pieces: list[str] = []
    for cand in getattr(response, "candidates", []) or []:
        content = getattr(cand, "content", None)
        parts = getattr(content, "parts", []) or []
        for part in parts:
            t = getattr(part, "text", "") or ""
            if t:
                pieces.append(t)
    return "\n".join(pieces).strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def ask_gemini(
    prompt: str,
    expect_json: bool = False,
    *,
    model: str = DEFAULT_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
    max_output_tokens: int = DEFAULT_MAX_TOKENS,
    max_retries: int | None = None,
) -> Any:
    """Send `prompt` to Gemini and return the result.

    Args:
        prompt: The full user prompt.
        expect_json: If True, request JSON-mode output and return a parsed
            Python object (dict or list). If False, return a clean string
            with any ``` fences stripped.
        model: Gemini model id. Default `gemini-2.0-flash` (free tier).
        temperature: 0 = deterministic, 1 = more creative.
        max_output_tokens: Per-call cap.

    Returns:
        str when `expect_json=False`, otherwise dict / list.

    Raises:
        RuntimeError on missing key, missing package, API failure, or empty response.
        ValueError when `expect_json=True` and the response can't be parsed.
    """
    _ensure_configured()
    retries_cap = _MAX_RETRIES if max_retries is None else max(1, min(12, int(max_retries)))

    generation_config: dict[str, Any] = {
        "temperature": float(temperature),
        "max_output_tokens": int(max_output_tokens),
    }
    if expect_json:
        # gemini-2.0-flash supports a hard JSON output mode.
        generation_config["response_mime_type"] = "application/json"

    alt = _resolve_fallback_model()
    model_chain: list[str] = []
    for mid in (model, alt, "gemini-2.5-flash", "gemini-flash-latest", "gemini-pro-latest", DEFAULT_MODEL):
        if mid and mid not in model_chain:
            model_chain.append(mid)

    last_exc: Exception | None = None
    response: Any = None

    for mid in model_chain:
        for attempt in range(retries_cap):
            try:
                gen_model = genai.GenerativeModel(mid)
                response = gen_model.generate_content(
                    prompt,
                    generation_config=generation_config,
                )
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                if _is_model_not_found(exc):
                    if mid != model_chain[-1]:
                        print(
                            f"[gemini] model={mid!r} not available; trying next model...",
                            file=sys.stderr,
                        )
                    break
                if not _is_retryable_quota(exc):
                    if mid != model_chain[-1]:
                        print(
                            f"[gemini] model={mid!r} hit non-retryable quota limit: {exc}; trying next model...",
                            file=sys.stderr,
                        )
                        break
                    raise RuntimeError(f"Gemini API call failed: {exc}") from exc
                if attempt + 1 >= retries_cap:
                    if mid != model_chain[-1]:
                        print(
                            f"[gemini] model={mid!r} still rate-limited after "
                            f"{retries_cap} attempt(s); trying next model...",
                            file=sys.stderr,
                        )
                    break
                delay = _retry_delay_seconds(exc, attempt)
                print(
                    f"[gemini] {type(exc).__name__} on {mid!r} "
                    f"(attempt {attempt + 1}/{retries_cap}); "
                    f"sleeping {delay:.1f}s — see "
                    "https://ai.google.dev/gemini-api/docs/rate-limits",
                    file=sys.stderr,
                )
                time.sleep(delay)
        if last_exc is None and response is not None:
            break

    if response is None and last_exc is not None:
        raise RuntimeError(
            "Gemini API call failed after retries (quota / rate limit). "
            "Wait a few minutes, try again, set `ai.fallback_model` in "
            "config.yaml to another Gemini id (e.g. gemini-2.0-flash-lite), or "
            "check billing / API enablement for your Cloud project.\n"
            f"Last error: {last_exc}"
        ) from last_exc

    text = _extract_text(response)
    if not text:
        raise RuntimeError("Gemini returned an empty response.")

    if expect_json:
        try:
            return _safe_json_parse(text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"Could not parse Gemini response as JSON: {exc}\n"
                f"Raw response (first 500 chars): {text[:500]}"
            ) from exc

    return _strip_fences(text)


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    try:
        out = ask_gemini(
            'Reply with the JSON {"ok": true, "library": "google-generativeai"} '
            "and nothing else.",
            expect_json=True,
        )
        print(out)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
