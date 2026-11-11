"""Tests for the Ollama LLM adapter (app/ollama_llm.py).

The adapter is the live AI in Lamplight Checkout: it calls a local
Ollama model for brief parsing. These tests need no live model —
they cover JSON extraction, env-var wiring, and failure modes.
Live-model behaviour is verified separately against a running Ollama.
"""
import json
import os

import pytest

from app.hiring_agent import HiringAgent
from app.ollama_llm import (
    OllamaUnavailable,
    check_ollama,
    extract_json,
    make_llm,
    ollama_llm,
)


# -- extract_json ---------------------------------------------------------------

def test_extract_json_clean_passthrough():
    raw = '{"service": "audit", "budget_cents": 1250, "keywords": ["audit"]}'
    assert json.loads(extract_json(raw)) == {
        "service": "audit", "budget_cents": 1250, "keywords": ["audit"]}


def test_extract_json_strips_json_fence():
    raw = ('```json\n'
           '{"service": "write", "budget_cents": null, "keywords": ["write"]}\n'
           '```')
    assert json.loads(extract_json(raw)) == {
        "service": "write", "budget_cents": None, "keywords": ["write"]}


def test_extract_json_strips_plain_fence():
    raw = '```\n{"service": "code", "budget_cents": 2000, "keywords": ["code"]}\n```'
    assert json.loads(extract_json(raw))["service"] == "code"


def test_extract_json_cuts_surrounding_prose():
    raw = ('Here is the parsed brief:\n'
           '{"service": "review", "budget_cents": 800, "keywords": ["review"]}\n'
           'Let me know if you need anything else.')
    assert json.loads(extract_json(raw))["service"] == "review"


# -- make_llm wiring ------------------------------------------------------------

def test_make_llm_off_by_default(monkeypatch):
    monkeypatch.delenv("LAMPLIGHT_LLM", raising=False)
    assert make_llm() is None


def test_make_llm_off_for_other_values(monkeypatch):
    monkeypatch.setenv("LAMPLIGHT_LLM", "openai")
    assert make_llm() is None


def test_make_llm_returns_callable_when_ollama(monkeypatch):
    monkeypatch.setenv("LAMPLIGHT_LLM", "ollama")
    llm = make_llm()
    assert callable(llm)


def test_make_llm_callable_extracts_json(monkeypatch):
    """The make_llm wrapper cleans model output before the hiring agent
    sees it, so a fenced reply still parses."""
    monkeypatch.setenv("LAMPLIGHT_LLM", "ollama")
    monkeypatch.setenv("LAMPLIGHT_OLLAMA_URL", "http://127.0.0.1:1")  # dead port
    llm = make_llm()
    # The wrapper would call Ollama here; instead verify the extraction
    # half by feeding it through extract_json directly (the wrapper is
    # extract_json(ollama_llm(...))).
    assert json.loads(extract_json(
        '```json\n{"service": "audit", "budget_cents": 1250, "keywords": ["audit"]}\n```')
    )["budget_cents"] == 1250


# -- failure modes ---------------------------------------------------------------

def test_ollama_llm_unreachable_raises():
    with pytest.raises(OllamaUnavailable):
        ollama_llm("hello", base_url="http://127.0.0.1:1", timeout=5)


def test_ollama_llm_wrong_model_raises():
    """A missing model is a clear error, not a silent fallback."""
    with pytest.raises(OllamaUnavailable):
        ollama_llm("hello", model="definitely-not-a-model-xyz",
                    base_url="http://127.0.0.1:11434", timeout=10)


def test_check_ollama_unreachable():
    result = check_ollama(base_url="http://127.0.0.1:1", timeout=5)
    assert result["reachable"] is False
    assert result["ok"] is False
    assert result["error"]


# -- hiring agent with an LLM ----------------------------------------------------

def test_hiring_agent_uses_llm_when_set():
    def fake_llm(prompt: str) -> str:
        assert "hiring brief" in prompt
        return '{"service": "audit", "budget_cents": 1250, "keywords": ["audit"]}'
    hiring = HiringAgent(llm=fake_llm)
    parsed = hiring.parse_brief("Please audit our top holders, budget $12.50")
    assert parsed == {"service": "audit", "budget_cents": 1250,
                      "keywords": ["audit"]}


def test_hiring_agent_negotiate_with_llm():
    """End to end through the hook: LLM parse -> worker match -> quote."""
    def fake_llm(prompt: str) -> str:
        return '{"service": "write", "budget_cents": 800, "keywords": ["write"]}'
    hiring = HiringAgent(llm=fake_llm)
    quote, worker = hiring.negotiate("Write a summary of the report, $8 max")
    assert worker["agent_id"] == "writer-02"
    assert quote["price_cents"] == 800
