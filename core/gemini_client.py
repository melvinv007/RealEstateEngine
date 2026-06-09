"""
gemini_client.py
Centralised AI caller with Gemini primary + Groq fallback.

Features:
- Uses google.genai (new SDK) for Gemini calls
- Tries each Gemini model in GEMINI_MODELS in order
- When all Gemini models exhausted → falls through to Groq models
- Daily quota exhausted → skip model immediately, blacklist for session
- Per-minute rate limit / 500 → retry with API-suggested wait time
- Bad model name / auth error → skip immediately
- Request logging to cache/gemini_calls.log (both Gemini and Groq)
- Prompt length warning
- Current model printed only on switch
- Groq does not support image_part — skipped automatically with warning
- JSON mode: Gemini uses response_mime_type, Groq uses response_format
"""

import base64
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

from core.config import (
    GEMINI_API_KEY,
    GEMINI_MODELS,
    GROQ_API_KEY,
    GROQ_MODELS,
    OPENROUTER_API_KEY,
    OPENROUTER_MODELS,
)
from core.config import PRODUCTION_MODE

try:
    from json_repair import repair_json
except Exception:
    repair_json = None

_GEMINI_CLIENT = None


def _get_gemini_client() -> genai.Client | None:
    global _GEMINI_CLIENT
    if _GEMINI_CLIENT is None and GEMINI_API_KEY:
        _GEMINI_CLIENT = genai.Client(api_key=GEMINI_API_KEY)
    return _GEMINI_CLIENT

# ── Constants ──────────────────────────────────────────────────────────────────
RETRY_MAX          = 3      # max attempts per model for transient errors
RETRY_WAIT_DEFAULT = 5.0    # fallback wait if API doesn't suggest one
PROMPT_LENGTH_WARN = 8000   # warn if prompt exceeds this many characters

# ── Session state ──────────────────────────────────────────────────────────────
_daily_quota_blacklist: set[str] = set()
_current_model: str | None = None
_groq_fallback_announced: bool = False

# ── Logging ────────────────────────────────────────────────────────────────────
_CALL_LOG = "cache/gemini_calls.log"


def _timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _old_log_call(model_name: str, success: bool, latency_s: float, note: str = "") -> None:
    # In production we suppress writes to gemini_calls.log to reduce disk noise
    if PRODUCTION_MODE:
        return
    path = Path(_CALL_LOG)
    path.parent.mkdir(parents=True, exist_ok=True)
    status = "OK" if success else "FAIL"
    line = f"{_timestamp()}\t{model_name}\t{status}\t{latency_s:.2f}s\t{note}"
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")

def _log_call(model_name: str, success: bool, latency_s: float, note: str = "", prompt_snippet: str = "") -> None:
    """Log Gemini/Groq/OpenRouter call result to MongoDB and optionally to file."""
    # File log (dev only, keep for startup debugging before DB is up)
    if not PRODUCTION_MODE:
        path = Path(_CALL_LOG)
        path.parent.mkdir(parents=True, exist_ok=True)
        status = "OK" if success else "FAIL"
        line = f"{_timestamp()}\t{model_name}\t{status}\t{latency_s:.2f}s\t{note}"
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    # MongoDB log
    if not success and note:
        try:
            from core.logger import log_gemini_error, log_gemini_success
            # Parse provider from model_name (e.g. "groq/llama3-70b" → "groq")
            if "/" in model_name:
                provider, model = model_name.split("/", 1)
            else:
                provider, model = "gemini", model_name
            attempt = 1
            import re as _re
            m = _re.search(r"attempt(\d+)", note)
            if m:
                attempt = int(m.group(1))
            log_gemini_error(
                model=model,
                provider=provider,
                error_type=note.split("_attempt")[0],
                error_message=note,
                attempt=attempt,
                latency_s=latency_s,
                prompt_snippet=prompt_snippet,
            )
        except Exception:
            pass
    elif success:
        try:
            from core.logger import log_gemini_success
            if "/" in model_name:
                provider, model = model_name.split("/", 1)
            else:
                provider, model = "gemini", model_name
            log_gemini_success(model=model, provider=provider, latency_s=latency_s)
        except Exception:
            pass

def _pprint(msg: str, level: str = "INFO") -> None:
    """
    Controlled printing helper. In production, only WARN and ERROR are printed.
    """
    lvl = (level or "INFO").upper()
    if PRODUCTION_MODE and lvl not in ("WARN", "ERROR"):
        return
    print(msg)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_retry_delay(err_str: str, default: float = RETRY_WAIT_DEFAULT) -> float:
    """Extract suggested retry delay from Gemini error message."""
    match = re.search(r'retry_delay\s*\{\s*seconds:\s*(\d+)', err_str)
    if match:
        return float(match.group(1)) + 1.0  # +1s buffer
    return default


def _prompt_length(prompt: Any) -> int:
    """Estimate character length of prompt regardless of type."""
    if isinstance(prompt, str):
        return len(prompt)
    if isinstance(prompt, list):
        return sum(len(str(p)) for p in prompt)
    return len(str(prompt))


def _prompt_to_string(prompt: Any) -> str:
    """Flatten prompt to a plain string for non-Gemini backends."""
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, list):
        parts = []
        for p in prompt:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                # skip inline_data image parts
                if "inline_data" not in p:
                    parts.append(str(p))
        return "\n".join(parts)
    return str(prompt)


def _strip_markdown_fences(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^```json", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"^```", "", cleaned).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    return cleaned


def _validate_json_text(raw_text: str) -> str | None:
    cleaned = _strip_markdown_fences(raw_text)
    try:
        json.loads(cleaned)
        return cleaned
    except Exception:
        if repair_json:
            try:
                repaired = repair_json(cleaned)
                json.loads(repaired)
                return repaired
            except Exception:
                pass
        _pprint(f"[Parser] Invalid JSON returned: {raw_text[:500]}", level="WARN")
        return None


def _normalize_contents(prompt: Any, image_part: dict | None) -> list[Any]:
    contents: list[Any] = []

    if isinstance(prompt, list):
        contents.extend(prompt)
    else:
        contents.append(prompt)

    if image_part:
        image_bytes = base64.b64decode(image_part["data"])
        contents.append(
            types.Part.from_bytes(
                data=image_bytes,
                mime_type=image_part["mime_type"],
            )
        )

    return contents


def _set_current_model(model_name: str) -> None:
    global _current_model
    if _current_model != model_name:
        _current_model = model_name
        _pprint(f"[AI] Now using model: {model_name}", level="INFO")


def _classify_gemini_error(err: str) -> str:
    """
    Classify Gemini API error into a category.
      daily_quota  — daily free-tier cap hit
      rate_limit   — per-minute quota
      server_err   — 500 internal
      bad_model    — 404 / not found
      auth_err     — 403 / billing / permission
      unknown      — anything else
    """
    if "PerDay" in err or "per_day" in err.lower():
        return "daily_quota"
    if "429" in err or "quota" in err.lower() or "resource_exhausted" in err.lower():
        return "rate_limit"
    if "500" in err or "internal error" in err.lower():
        return "server_err"
    if "404" in err or "not found" in err.lower() or "not supported" in err.lower():
        return "bad_model"
    if "403" in err or "permission" in err.lower() or "billing" in err.lower() or "api key" in err.lower():
        return "auth_err"
    return "unknown"


def _classify_groq_error(err: str) -> str:
    """
    Classify Groq API error into a category.
      daily_quota  — daily token/request cap
      rate_limit   — per-minute quota
      server_err   — 500 / service unavailable
      bad_model    — model not found
      auth_err     — invalid API key
      unknown      — anything else
    """
    err_lower = err.lower()
    if "per day" in err_lower or "daily" in err_lower or "day_tokens" in err_lower:
        return "daily_quota"
    if "429" in err or "rate limit" in err_lower or "too many requests" in err_lower:
        return "rate_limit"
    if "500" in err or "502" in err or "503" in err or "service unavailable" in err_lower:
        return "server_err"
    if "404" in err or "model not found" in err_lower or "does not exist" in err_lower:
        return "bad_model"
    if "401" in err or "invalid api key" in err_lower or "authentication" in err_lower:
        return "auth_err"
    return "unknown"

def _classify_openrouter_error(err: str) -> str:
    """
    Classify OpenRouter API errors.
    """
    err_lower = err.lower()

    if "rate limit" in err_lower or "429" in err:
        return "rate_limit"

    if "402" in err or "insufficient credits" in err_lower:
        return "daily_quota"

    if (
        "500" in err
        or "502" in err
        or "503" in err
        or "server error" in err_lower
    ):
        return "server_err"

    if (
        "404" in err
        or "model not found" in err_lower
        or "no endpoints found" in err_lower
    ):
        return "bad_model"

    if (
        "401" in err
        or "invalid api key" in err_lower
        or "authentication" in err_lower
    ):
        return "auth_err"

    return "unknown"

# ── Groq response wrapper ──────────────────────────────────────────────────────

class _GroqResponse:
    """
    Wraps Groq response to match the .text interface callers expect
    (same as Gemini's response.text).
    """
    def __init__(self, text: str):
        self.text = text

class _OpenRouterResponse:
    """
    Wrap OpenRouter response to mimic Gemini's .text interface.
    """

    def __init__(self, text: str):
        self.text = text


# ── Gemini caller ──────────────────────────────────────────────────────────────

def _call_gemini_models(
    prompt: Any,
    generation_config: dict | None,
    system_instruction: str | None,
    image_part: dict | None,
) -> Any | None:
    """
    Try all Gemini models. Returns response or None if all exhausted.
    """
    last_error = None

    for model_name in GEMINI_MODELS:
        if model_name in _daily_quota_blacklist:
            continue

        _set_current_model(model_name)

        kwargs = {}
        if system_instruction:
            kwargs["system_instruction"] = system_instruction

        config_kwargs: dict[str, Any] = dict(generation_config or {})
        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction
        config = types.GenerateContentConfig(**config_kwargs)

        contents = _normalize_contents(prompt, image_part)
        client = _get_gemini_client()
        if client is None:
            return None

        for attempt in range(1, RETRY_MAX + 1):
            t_start = time.time()
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=config,
                )

                latency = time.time() - t_start
                _log_call(f"gemini/{model_name}", True, latency, prompt_snippet=str(prompt)[:200])
                return response

            except Exception as e:
                latency = time.time() - t_start
                err = str(e)
                category = _classify_gemini_error(err)

                if category == "daily_quota":
                    _pprint(f"[Gemini] {model_name} daily quota exhausted — blacklisting for this session.", level="WARN")
                    _log_call(f"gemini/{model_name}", False, latency, "daily_quota", prompt_snippet=str(prompt)[:200])
                    _daily_quota_blacklist.add(model_name)
                    last_error = e
                    break

                elif category in ("bad_model", "auth_err"):
                    _pprint(f"[Gemini] {model_name} skipped — {category}: {err[:100]}", level="WARN")
                    _log_call(f"gemini/{model_name}", False, latency, category, prompt_snippet=str(prompt)[:200])
                    last_error = e
                    break

                elif category in ("rate_limit", "server_err"):
                    _pprint(f"[Gemini] {model_name} attempt {attempt}/{RETRY_MAX} ({category}): {err[:120]}", level="WARN")
                    _log_call(f"gemini/{model_name}", False, latency, f"{category}_attempt{attempt}", prompt_snippet=str(prompt)[:200])
                    last_error = e
                    if attempt < RETRY_MAX:
                        wait = _extract_retry_delay(err)
                        _pprint(f"[Gemini] Waiting {wait:.0f}s before retry...", level="WARN")
                        time.sleep(wait)

                else:
                    _log_call(f"gemini/{model_name}", False, latency, f"unknown: {err[:80]}", prompt_snippet=str(prompt)[:200])
                    raise

        else:
            _pprint(f"[Gemini] {model_name} exhausted all {RETRY_MAX} attempts, trying next model...", level="DEBUG")

    return None  # all Gemini models exhausted


# ── Groq caller ───────────────────────────────────────────────────────────────

def _call_groq_models(
    prompt: Any,
    generation_config: dict | None,
    system_instruction: str | None,
    image_part: dict | None,
) -> Any | None:
    """
    Try all Groq models. Returns _GroqResponse or None if all exhausted.
    Groq does not support image input — skipped with warning if image_part present.
    """
    if not GROQ_API_KEY:
        return None

    if not GROQ_MODELS:
        return None

    if image_part:
        _pprint("[Groq] Warning: image_part not supported by Groq — skipping Groq fallback.", level="WARN")
        return None

    try:
        from groq import Groq
    except ImportError:
        _pprint("[Groq] groq package not installed. Run: pip install groq", level="WARN")
        return None

    client = Groq(api_key=GROQ_API_KEY)
    prompt_text = _prompt_to_string(prompt)

    # Build messages
    messages = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    messages.append({"role": "user", "content": prompt_text})

    # Groq JSON mode — enabled when Gemini's response_mime_type was application/json
    wants_json = (
        isinstance(generation_config, dict)
        and generation_config.get("response_mime_type") == "application/json"
    )

    last_error = None

    for model_name in GROQ_MODELS:
        if model_name in _daily_quota_blacklist:
            continue

        _set_current_model(f"groq/{model_name}")

        for attempt in range(1, RETRY_MAX + 1):
            t_start = time.time()
            try:
                kwargs: dict[str, Any] = {
                    "model": model_name,
                    "messages": messages,
                    "temperature": 0.1,
                }
                if wants_json:
                    kwargs["response_format"] = {"type": "json_object"}

                completion = client.chat.completions.create(**kwargs)
                text = completion.choices[0].message.content or ""

                latency = time.time() - t_start
                if wants_json:
                    validated = _validate_json_text(text)
                    if validated is None:
                        _log_call(f"groq/{model_name}", False, latency, "invalid_json")
                        continue
                    text = validated

                _log_call(f"groq/{model_name}", True, latency, prompt_snippet=str(prompt)[:200])
                return _GroqResponse(text)

            except Exception as e:
                latency = time.time() - t_start
                err = str(e)
                category = _classify_groq_error(err)

                if category == "daily_quota":
                    _pprint(f"[Groq] {model_name} daily quota exhausted — blacklisting for this session.", level="WARN")
                    _log_call(f"groq/{model_name}", False, latency, "daily_quota", prompt_snippet=str(prompt)[:200])
                    _daily_quota_blacklist.add(model_name)
                    last_error = e
                    break

                elif category in ("bad_model", "auth_err"):
                    _pprint(f"[Groq] {model_name} skipped — {category}: {err[:100]}", level="WARN")
                    _log_call(f"groq/{model_name}", False, latency, category, prompt_snippet=str(prompt)[:200])
                    last_error = e
                    break

                elif category in ("rate_limit", "server_err"):
                    _pprint(f"[Groq] {model_name} attempt {attempt}/{RETRY_MAX} ({category}): {err[:120]}", level="WARN")
                    _log_call(f"groq/{model_name}", False, latency, f"{category}_attempt{attempt}", prompt_snippet=str(prompt)[:200])
                    last_error = e
                    if attempt < RETRY_MAX:
                        wait = _extract_retry_delay(err)
                        _pprint(f"[Groq] Waiting {wait:.0f}s before retry...", level="WARN")
                        time.sleep(wait)

                else:
                    _log_call(f"groq/{model_name}", False, latency, f"unknown: {err[:80]}", prompt_snippet=str(prompt)[:200])
                    raise

        else:
            _pprint(f"[Groq] {model_name} exhausted all {RETRY_MAX} attempts, trying next model...", level="DEBUG")

    return None  # all Groq models exhausted

def _call_openrouter_models(
    prompt: Any,
    generation_config: dict | None,
    system_instruction: str | None,
    image_part: dict | None,
) -> Any | None:
    """
    Try all OpenRouter models.
    Returns _OpenRouterResponse or None if exhausted.
    """

    if not OPENROUTER_API_KEY:
        return None

    if not OPENROUTER_MODELS:
        return None

    if image_part:
        _pprint("[OpenRouter] Warning: image_part not supported — skipping.", level="WARN")
        return None

    try:
        from openai import OpenAI
    except ImportError:
        _pprint("[OpenRouter] openai package not installed.", level="WARN")
        return None

    client = OpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
    )

    prompt_text = _prompt_to_string(prompt)

    messages = []

    if system_instruction:
        messages.append({
            "role": "system",
            "content": system_instruction,
        })

    messages.append({
        "role": "user",
        "content": prompt_text,
    })

    wants_json = (
        isinstance(generation_config, dict)
        and generation_config.get("response_mime_type") == "application/json"
    )

    for model_name in OPENROUTER_MODELS:

        if f"openrouter/{model_name}" in _daily_quota_blacklist:
            continue

        _set_current_model(f"openrouter/{model_name}")

        for attempt in range(1, RETRY_MAX + 1):

            t_start = time.time()

            try:

                kwargs: dict[str, Any] = {
                    "model": model_name,
                    "messages": messages,
                    "temperature": 0.1,
                }

                if wants_json:
                    kwargs["response_format"] = {
                        "type": "json_object"
                    }

                completion = client.chat.completions.create(**kwargs)

                text = completion.choices[0].message.content or ""

                latency = time.time() - t_start

                if wants_json:
                    validated = _validate_json_text(text)
                    if validated is None:
                        _log_call(
                            f"openrouter/{model_name}",
                            False,
                            latency,
                            "invalid_json",
                        )
                        continue
                    text = validated

                _log_call(f"openrouter/{model_name}", True, latency, prompt_snippet=str(prompt)[:200])

                return _OpenRouterResponse(text)

            except Exception as e:

                latency = time.time() - t_start
                err = str(e)

                category = _classify_openrouter_error(err)

                if category == "daily_quota":
                    _pprint(f"[OpenRouter] {model_name} quota exhausted.", level="WARN")
                    _log_call(f"openrouter/{model_name}", False, latency, "daily_quota", prompt_snippet=str(prompt)[:200])
                    _daily_quota_blacklist.add(model_name)
                    break

                elif category in ("bad_model", "auth_err"):
                    _pprint(f"[OpenRouter] {model_name} skipped — {category}: {err[:100]}", level="WARN")
                    _log_call(f"openrouter/{model_name}", False, latency, category, prompt_snippet=str(prompt)[:200])
                    last_error = e
                    break

                elif category in ("rate_limit", "server_err"):
                    _pprint(f"[OpenRouter] {model_name} attempt {attempt}/{RETRY_MAX} ({category}): {err[:120]}", level="DEBUG")
                    _log_call(f"openrouter/{model_name}", False, latency, f"{category}_attempt{attempt}", prompt_snippet=str(prompt)[:200])
                    last_error = e
                    if attempt < RETRY_MAX:
                        wait = _extract_retry_delay(err)
                        _pprint(f"[OpenRouter] Waiting {wait:.0f}s before retry...", level="WARN")
                        time.sleep(wait)

                else:
                    _log_call(
                        f"openrouter/{model_name}",
                        False,
                        latency,
                        f"unknown: {err[:80]}",
                        prompt_snippet=str(prompt)[:200]
                    )
                    raise

    return None

# ── Public interface ───────────────────────────────────────────────────────────

def call_gemini(
    prompt: Any,
    generation_config: dict | None = None,
    system_instruction: str | None = None,
    image_part: dict | None = None,
) -> Any:
    """
    Main entry point. Tries Gemini models first, then Groq as fallback.
    Returns a response object with a .text attribute.
    Raises if all models across all providers are exhausted.
    """
    global _groq_fallback_announced
    if not GEMINI_API_KEY and not GROQ_API_KEY:
        raise ValueError("[AI] Neither GEMINI_API_KEY nor GROQ_API_KEY is set.")

    # Warn on very long prompts
    length = _prompt_length(prompt)
    if length > PROMPT_LENGTH_WARN:
        _pprint(f"[AI] Warning: prompt is {length} chars — may hit token limits.", level="WARN")

    try:

        # Try Gemini first
        if GEMINI_API_KEY:
            response = _call_gemini_models(prompt, generation_config, system_instruction, image_part)
            if response is not None:
                return response

        if not _groq_fallback_announced:
            _pprint("[AI] All Gemini models exhausted — falling back to Groq for this session.", level="WARN")
            _groq_fallback_announced = True

        # Fall through to Groq
        response = _call_groq_models(
            prompt,
            generation_config,
            system_instruction,
            image_part,
        )

        if response is not None:
            return response

        # Fall through to OpenRouter
        _pprint("[AI] Groq exhausted — falling back to OpenRouter.", level="WARN")

        response = _call_openrouter_models(
            prompt,
            generation_config,
            system_instruction,
            image_part,
        )

        if response is not None:
            return response

        raise Exception(
            "[AI] All models exhausted across Gemini, Groq, and OpenRouter."
        )
    except KeyboardInterrupt:
        print("\n[AI] Interrupted by user. Exiting cleanly.")
        raise


def get_blacklisted_models() -> set[str]:
    """Returns models blacklisted this session due to daily quota."""
    return set(_daily_quota_blacklist)


def reset_blacklist() -> None:
    """Clears the session blacklist. Useful for long-running processes spanning midnight."""
    global _groq_fallback_announced
    _daily_quota_blacklist.clear()
    _groq_fallback_announced = False
    _pprint("[AI] Session blacklist cleared.", level="INFO")