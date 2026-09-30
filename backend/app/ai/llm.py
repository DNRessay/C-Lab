# Cohere (suggestions) and Groq (chat), over plain HTTP.
import json
import logging
import re

import requests

from ..config import settings

log = logging.getLogger(__name__)

COHERE_URL = "https://api.cohere.com/v2/chat"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
NOTE = ("You help one person in South Africa understand their own money. Amounts are in rand unless stated. "
        "Be specific: use their numbers. You are not a licensed financial adviser; say so briefly when advice is "
        "about buying or selling particular shares.")


class AIError(Exception):
    pass


def cohere(system: str, user: str, json_mode=False) -> str:
    if not settings.cohere_api_key:
        raise AIError("Cohere isn't set up yet (add the COHERE_API_KEY secret).")
    body = {"model": settings.cohere_model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.3}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    r = requests.post(COHERE_URL, json=body, timeout=60,
                      headers={"Authorization": f"Bearer {settings.cohere_api_key}", "Content-Type": "application/json"})
    if not r.ok:
        log.warning("Cohere %s: %s", r.status_code, r.text[:300])
        raise AIError(f"Cohere didn't answer (HTTP {r.status_code}).")
    parts = (r.json().get("message") or {}).get("content") or []
    return "".join(p.get("text", "") for p in parts if p.get("type") == "text")


_next_key = 0


def groq(messages, max_tokens=900, json_mode=False) -> str:
    """Chat completion; on a rate limit, the next key is tried."""
    global _next_key
    keys = settings.groq_api_keys
    if not keys:
        raise AIError("Groq isn't set up yet (add the GROQ_API_KEYS secret).")
    last = ""
    for i in range(len(keys)):
        key = keys[(_next_key + i) % len(keys)]
        r = requests.post(GROQ_URL, timeout=60, headers={"Authorization": f"Bearer {key}"},
                          json={"model": settings.groq_model, "messages": messages, "temperature": 0 if json_mode else 0.4,
                                "max_tokens": max_tokens, **({"response_format": {"type": "json_object"}} if json_mode else {})})
        if r.status_code == 429:
            last = "rate limited"
            continue
        if not r.ok:
            log.warning("Groq %s: %s", r.status_code, r.text[:300])
            raise AIError(f"Groq didn't answer (HTTP {r.status_code}).")
        _next_key = (_next_key + i + 1) % len(keys)
        return r.json()["choices"][0]["message"]["content"]
    raise AIError(f"Groq is busy ({last}); try again in a minute.")


def parse_json(text: str):
    text = text.strip()
    m = re.search(r"\{.*\}|\[.*\]", text, re.S)
    try:
        return json.loads(m.group(0) if m else text)
    except (ValueError, AttributeError):
        return None
