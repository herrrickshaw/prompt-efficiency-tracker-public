"""provider.py imports llm_extract (lives in market-pipeline, not here); stub it."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def provider(monkeypatch):
    calls = []

    def fake_extract(prompt, schema, **kw):
        calls.append((prompt, schema, kw))
        if schema == "boom":
            raise RuntimeError("provider down")
        return {"ok": True}

    monkeypatch.setitem(sys.modules, "llm_extract", types.SimpleNamespace(extract_structured=fake_extract))
    spec = importlib.util.spec_from_file_location("pf_provider", REPO / "promptfoo" / "market-pipeline" / "provider.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.calls = calls
    return mod


def test_parse_extract_config_json_string_and_dict(provider):
    assert provider.parse_extract_config({"vars": {"extract_config": '{"schema": {"a": "b"}}'}}) == {
        "schema": {"a": "b"}
    }
    assert provider.parse_extract_config({"vars": {"extract_config": {"schema": 1}}}) == {"schema": 1}
    assert provider.parse_extract_config(None) == {}


def test_call_api_passes_config_and_defaults(provider):
    ctx = {"vars": {"extract_config": json.dumps({"schema": {"x": "number"}, "require_citations": False})}}
    out = provider.call_api("doc text", {}, ctx)
    assert json.loads(out["output"]) == {"ok": True}
    prompt, schema, kw = provider.calls[0]
    assert (prompt, schema) == ("doc text", {"x": "number"})
    assert kw == {"instructions": "", "provider": "groq", "fallback_provider": "gemini", "require_citations": False}


def test_call_api_reports_errors(provider):
    out = provider.call_api("doc", {}, {"vars": {"extract_config": '{"schema": "boom"}'}})
    assert out == {"output": "", "error": "provider down"}
