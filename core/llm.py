"""
Thin wrapper around any OpenAI-compatible chat API (OpenAI, Groq, Gemini, OpenRouter,
Together, Ollama, ...). Configure with environment variables / .env:

    LLM_API_KEY=...
    LLM_BASE_URL=https://api.groq.com/openai/v1      # optional, omit for OpenAI
    LLM_MODEL=llama-3.3-70b-versatile

If no key is set the app still works end-to-end using deterministic fallbacks
(template SQL + template explanations), so a demo never breaks.
"""
import json
import os
import re

from dotenv import load_dotenv

load_dotenv()

_client = None


def available() -> bool:
    return bool(os.getenv("LLM_API_KEY"))


def model_name() -> str:
    return os.getenv("LLM_MODEL", "gpt-4o-mini")


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(api_key=os.getenv("LLM_API_KEY"), base_url=os.getenv("LLM_BASE_URL") or None)
    return _client


def chat(system: str, user: str, temperature: float = 0.1, max_tokens: int = 800) -> str:
    resp = _get_client().chat.completions.create(
        model=model_name(),
        temperature=temperature,
        max_tokens=max_tokens,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    return resp.choices[0].message.content.strip()


def chat_json(system: str, user: str) -> dict:
    txt = chat(system + "\nRespond with ONLY a valid JSON object, no prose, no markdown fences.", user)
    m = re.search(r"\{.*\}", txt, re.S)
    return json.loads(m.group(0) if m else txt)
