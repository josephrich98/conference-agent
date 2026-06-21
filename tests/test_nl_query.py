"""Offline tests for natural-language → boolean-query translation.

The local LLM call (``web.nl_query._call_ollama``) is monkeypatched so these run
hermetically with no Ollama server. A live, end-to-end translation test would be
marked ``@pytest.mark.network`` / ``llm`` and is intentionally omitted from CI.
"""

from __future__ import annotations

import json

import pytest

from web import nl_query
from web.nl_query import (
    LLMUnavailable,
    Translation,
    TranslationError,
    _extract_query,
    _field_reference,
    translate,
)
from web.search import build_filter


def _fake_call(query):
    """Return a stand-in for ``_call_ollama`` that always answers with ``query``."""

    def _call(messages, *, model, base_url, timeout):
        return json.dumps({"query": query})

    return _call


def test_field_reference_lists_fields_and_vocabularies():
    ref = _field_reference()
    # Mirrors the search registry: scoped fields and a controlled vocabulary.
    assert "subcategory" in ref
    assert "conference_dates (date)" in ref
    assert "abstract_month" in ref
    assert "remote" in ref and "virtual" in ref  # the remote vocab is inlined


def test_prompt_teaches_month_range_and_any_year_handling():
    # Guidance that steers small models away from inventing a year and toward the
    # month fields for season / "any year" phrasing, including wrap-around ranges.
    prompt = nl_query._SYSTEM_PROMPT.format(fields=_field_reference())
    assert "MONTH field" in prompt
    assert "Never invent a year" in prompt
    # Both range shapes are demonstrated: in-year (AND) and wrap-around (OR).
    assert "conference_month:>=9 OR conference_month:<=1" in prompt
    assert "conference_month:>=3 AND conference_month:<=6" in prompt


def test_wrap_around_month_range_query_is_valid(monkeypatch):
    # The exact shape the prompt now teaches for "September through January of any
    # year" must survive validation and be returned unchanged.
    q = "size:large AND subcategory:radiology AND (conference_month:>=9 OR conference_month:<=1)"
    monkeypatch.setattr(nl_query, "_call_ollama", _fake_call(q))
    result = translate("big radiology conferences between September and January of any year")
    assert result.query == q
    assert result.repaired is False
    build_filter(result.query)


def test_extract_query_reads_query_field():
    assert _extract_query('{"query": "size:large"}') == "size:large"
    assert _extract_query('{"query": ""}') == ""


def test_extract_query_rejects_non_json():
    with pytest.raises(TranslationError):
        _extract_query("size:large (not json)")


def test_extract_query_rejects_non_string_query():
    with pytest.raises(TranslationError):
        _extract_query('{"query": 5}')


def test_empty_input_short_circuits(monkeypatch):
    # No LLM call should happen for blank input.
    called = False

    def _boom(*a, **k):
        nonlocal called
        called = True
        raise AssertionError("should not call the LLM")

    monkeypatch.setattr(nl_query, "_call_ollama", _boom)
    result = translate("   ")
    assert result == Translation(query="", natural_language="")
    assert called is False


def test_happy_path_returns_validated_query(monkeypatch):
    q = 'subcategory:radiology AND size:large AND remote:virtual'
    monkeypatch.setattr(nl_query, "_call_ollama", _fake_call(q))
    result = translate("big virtual radiology conferences")
    assert result.query == q
    assert result.repaired is False
    build_filter(result.query)  # the returned query really parses


def test_repair_round_recovers_from_bad_first_query(monkeypatch):
    calls = {"n": 0}

    def _call(messages, *, model, base_url, timeout):
        calls["n"] += 1
        # First attempt: an unknown field that fails to parse. Second: valid.
        bad = '{"query": "bogus_field:radiology"}'
        good = '{"query": "subcategory:radiology"}'
        return bad if calls["n"] == 1 else good

    monkeypatch.setattr(nl_query, "_call_ollama", _call)
    result = translate("radiology conferences")
    assert result.query == "subcategory:radiology"
    assert result.repaired is True
    assert calls["n"] == 2


def test_repair_exhausted_raises_translation_error(monkeypatch):
    monkeypatch.setattr(nl_query, "_call_ollama", _fake_call('{"query": "bogus_field:x"}'))
    with pytest.raises(TranslationError):
        translate("nonsense")


def test_llm_unavailable_propagates(monkeypatch):
    def _down(*a, **k):
        raise LLMUnavailable("server down")

    monkeypatch.setattr(nl_query, "_call_ollama", _down)
    with pytest.raises(LLMUnavailable):
        translate("anything")


def test_endpoint_maps_errors_to_status_codes(monkeypatch):
    from fastapi import HTTPException

    from web import app as webapp

    # 503 when the local model is unreachable.
    monkeypatch.setattr(webapp, "translate", lambda q: (_ for _ in ()).throw(LLMUnavailable("down")))
    with pytest.raises(HTTPException) as exc:
        webapp.api_translate(q="x")
    assert exc.value.status_code == 503

    # 422 when no valid query could be produced.
    monkeypatch.setattr(webapp, "translate", lambda q: (_ for _ in ()).throw(TranslationError("bad")))
    with pytest.raises(HTTPException) as exc:
        webapp.api_translate(q="x")
    assert exc.value.status_code == 422

    # Success returns the translated query payload.
    monkeypatch.setattr(webapp, "translate", lambda q: Translation(query="size:large", natural_language=q))
    out = webapp.api_translate(q="big ones")
    assert out["query"] == "size:large"
    assert out["repaired"] is False
