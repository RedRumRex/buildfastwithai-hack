"""
Thin wrapper around any OpenAI-compatible chat API (OpenAI, Groq, Gemini, OpenRouter, Ollama ...).

.env
    LLM_API_KEY=...
    LLM_BASE_URL=https://api.groq.com/openai/v1                          # Groq
    # LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/   # Gemini
    # (omit LLM_BASE_URL for OpenAI)
    LLM_MODEL=llama-3.3-70b-versatile        # Gemini: gemini-2.0-flash  OpenAI: gpt-4o-mini
    LLM_TIMEOUT=20                           # seconds per request
    LLM_COOLDOWN=60                          # pause LLM after repeated failures (seconds)

Failure handling: short timeout, retry with backoff on rate limits / 5xx / timeouts, then a
circuit breaker turns available() False for LLM_COOLDOWN seconds so the app falls back to
rules instantly instead of hanging on every click. Every failure raises LLMError.
"""
import json
import os
import re
import time

from dotenv import load_dotenv

load_dotenv()

_client = None
_down_until = 0.0
_last_error = ""


class LLMError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.getenv("LLM_API_KEY"))


def available() -> bool:
    return configured() and time.time() >= _down_until


def last_error() -> str:
    return _last_error


def model_name() -> str:
    return os.getenv("LLM_MODEL", "gpt-4o-mini")


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(api_key=os.getenv("LLM_API_KEY"), base_url=os.getenv("LLM_BASE_URL") or None,
                         timeout=float(os.getenv("LLM_TIMEOUT", "20")), max_retries=0)
    return _client


def _trip(reason: str, seconds: float | None = None):
    global _down_until, _last_error
    _last_error = reason
    _down_until = time.time() + (seconds if seconds is not None else float(os.getenv("LLM_COOLDOWN", "60")))


def _retry_after(e) -> float | None:
    try:
        return float(e.response.headers.get("retry-after"))
    except Exception:
        return None


def chat(system: str, user: str, temperature: float = 0.1, max_tokens: int = 800, retries: int = 2) -> str:
    global _last_error
    from openai import (APIConnectionError, APIStatusError, AuthenticationError, PermissionDeniedError,
                        RateLimitError)
    if not configured():
        raise LLMError("no LLM_API_KEY set")
    if not available():
        raise LLMError(f"LLM paused after error: {_last_error}")
    delay = 1.5
    for attempt in range(retries + 1):
        last = attempt == retries
        try:
            extra = {}
            if "gpt-oss" in model_name():
                extra["reasoning_effort"] = os.getenv("LLM_REASONING_EFFORT", "low")
            resp = _get_client().chat.completions.create(
                model=model_name(), temperature=temperature,
                max_tokens=max_tokens + (1024 if extra else 0),   # headroom for reasoning tokens
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                **extra)
            txt = (resp.choices[0].message.content or "").strip()
            txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()   # qwen-style thinking
            if not txt:
                raise LLMError("empty response from model")
            return txt
        except RateLimitError as e:
            wait = _retry_after(e) or delay
            if not last and wait <= 10:
                time.sleep(wait); delay *= 2; continue
            _trip("rate limited", max(wait, 20)); raise LLMError("rate limited") from e
        except (AuthenticationError, PermissionDeniedError) as e:
            _trip("invalid API key / no access", 3600); raise LLMError("invalid API key / no access") from e
        except APIConnectionError as e:          # includes APITimeoutError
            if not last:
                time.sleep(delay); delay *= 2; continue
            _trip(f"timeout/connection: {type(e).__name__}"); raise LLMError(_last_error) from e
        except APIStatusError as e:
            if e.status_code >= 500 and not last:
                time.sleep(delay); delay *= 2; continue
            if e.status_code >= 500:
                _trip(f"provider error {e.status_code}")
            _last_error = f"HTTP {e.status_code}: {str(e)[:150]}"
            raise LLMError(_last_error) from e
    raise LLMError("unreachable")


def _parse_json(txt: str) -> dict:
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", txt, re.S)
    return json.loads(m.group(0) if m else txt)


def chat_json(system: str, user: str, max_tokens: int = 800) -> dict:
    sys_msg = system + "\nRespond with ONLY a valid JSON object, no prose, no markdown fences."
    txt = chat(sys_msg, user, max_tokens=max_tokens)
    try:
        return _parse_json(txt)
    except (json.JSONDecodeError, ValueError):
        txt = chat(sys_msg, user + "\n\nYour previous reply was not valid JSON. Return ONLY the JSON object.",
                   temperature=0.0, max_tokens=max_tokens)
        try:
            return _parse_json(txt)
        except (json.JSONDecodeError, ValueError) as e:
            raise LLMError("model did not return valid JSON") from e
