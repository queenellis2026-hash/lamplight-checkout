"""Ollama LLM adapter for Lamplight Checkout's hiring agent.

This is the live AI inside the project: when wired in, the hiring agent's
brief parsing is done by a real language model (qwen2.5:7b served locally
by Ollama) instead of the deterministic regex parser. The model runs when
someone uses the app — every brief goes through it.

Usage:
    from app.ollama_llm import make_llm
    hiring = HiringAgent(llm=make_llm())   # None when Ollama is not enabled

Enable with env vars (all optional, defaults shown):
    LAMPLIGHT_LLM=ollama            # enable the Ollama LLM (default: off)
    LAMPLIGHT_OLLAMA_MODEL=qwen2.5:7b
    LAMPLIGHT_OLLAMA_URL=http://127.0.0.1:11434
    LAMPLIGHT_LLM_TIMEOUT=120       # seconds per model call

The callable has the signature the hiring agent expects:
    llm(prompt: str) -> str
and returns the model's raw text. JSON extraction (stripping markdown
fences and surrounding prose) happens here, so the hiring agent always
receives clean JSON to parse.
"""
from __future__ import annotations

import json
import os
import re

import requests

DEFAULT_MODEL = "qwen2.5:7b"
DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT = 120


class OllamaUnavailable(RuntimeError):
    """Raised when the Ollama server cannot be reached or has no model."""


def extract_json(raw: str) -> str:
    """Pull a JSON object out of model output.

    Models often wrap JSON in ```json fences or add a sentence around it.
    This strips fences and, failing that, returns the substring from the
    first '{' to the last '}' so json.loads gets a clean object.
    """
    text = raw.strip()
    # Strip markdown code fences (```json ... ``` or ``` ... ```).
    fence = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    # If there is prose around the object, cut to the outermost braces.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    return text


def ollama_llm(prompt: str, *, model: str = DEFAULT_MODEL,
               base_url: str = DEFAULT_URL,
               timeout: int = DEFAULT_TIMEOUT) -> str:
    """Call a local Ollama model and return its raw text response.

    Uses /api/generate with streaming off and temperature 0 so the
    brief parse is deterministic. Raises OllamaUnavailable when the
    server is down or the model is missing.
    """
    url = base_url.rstrip("/") + "/api/generate"
    try:
        resp = requests.post(url, json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0},
        }, timeout=timeout)
    except requests.RequestException as exc:
        raise OllamaUnavailable(
            f"Ollama server not reachable at {base_url}: {exc}") from exc
    if resp.status_code != 200:
        raise OllamaUnavailable(
            f"Ollama returned HTTP {resp.status_code} for model {model}: "
            f"{resp.text[:300]}")
    try:
        return resp.json()["response"]
    except (ValueError, KeyError) as exc:
        raise OllamaUnavailable(
            f"Ollama returned an unexpected body: {resp.text[:300]}") from exc


def make_llm() -> object | None:
    """Build the hiring agent's llm callable from env vars.

    Returns None when LAMPLIGHT_LLM is not set to 'ollama', so the
    hiring agent falls back to its deterministic parser (tests and
    offline demos keep working unchanged). When enabled, returns a
    callable(prompt) -> str that calls Ollama and extracts clean JSON.
    """
    if os.environ.get("LAMPLIGHT_LLM", "").strip().lower() != "ollama":
        return None
    model = os.environ.get("LAMPLIGHT_OLLAMA_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    base_url = os.environ.get("LAMPLIGHT_OLLAMA_URL", DEFAULT_URL).strip() or DEFAULT_URL
    try:
        timeout = int(os.environ.get("LAMPLIGHT_LLM_TIMEOUT", str(DEFAULT_TIMEOUT)))
    except ValueError:
        timeout = DEFAULT_TIMEOUT

    def _llm(prompt: str) -> str:
        raw = ollama_llm(prompt, model=model, base_url=base_url, timeout=timeout)
        return extract_json(raw)

    return _llm


def check_ollama(*, model: str = DEFAULT_MODEL,
                 base_url: str = DEFAULT_URL,
                 timeout: int = 10) -> dict:
    """Probe the Ollama server: reachable, and is the model loaded?

    Returns {"ok": bool, "reachable": bool, "model_present": bool,
    "models": [names], "error": str|None}. Never raises.
    """
    try:
        resp = requests.get(base_url.rstrip("/") + "/api/tags", timeout=timeout)
        resp.raise_for_status()
        models = [m.get("name", "") for m in resp.json().get("models", [])]
        return {"ok": model in models, "reachable": True,
                "model_present": model in models, "models": models,
                "error": None}
    except requests.RequestException as exc:
        return {"ok": False, "reachable": False, "model_present": False,
                "models": [], "error": str(exc)}
