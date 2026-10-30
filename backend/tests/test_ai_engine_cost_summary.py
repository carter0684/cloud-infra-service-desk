"""
Tests for app/ai_engine.py's Cost Analysis executive-summary generation
(analyse_cost_summary) and its JSON-extraction helper (_extract_json).

`_get_client()` / `_invoke()` are monkeypatched so no real Bedrock call is
ever made — no network access, no boto3 bedrock-runtime client construction.
"""

from __future__ import annotations

import json

import pytest

from app import ai_engine


def _cost_summary(**overrides) -> dict:
    base = {
        "currency": "USD",
        "total_mtd_cost": 1234.56,
        "total_last_month_cost": 1000.0,
        "total_two_months_ago_cost": 900.0,
        "mtd_vs_last_month_pct": 23.5,
        "last_month_vs_prior_pct": 11.1,
        "account_count": 3,
        "error_count": 0,
        "as_of": "2026-08-01T00:00:00Z",
        "business_units": [
            {"bu": "BU-A", "mtd_cost": 800.0, "last_month_cost": 700.0, "account_count": 2},
        ],
        "accounts": [
            {"account_id": "111111111111", "account_name": "AcctA", "mtd_cost": 800.0, "mtd_vs_last_month_pct": 10.0, "error": None},
        ],
        "top_services_mtd": [{"service": "Amazon EC2", "amount": 500.0}],
    }
    base.update(overrides)
    return base


# ── _extract_json ─────────────────────────────────────────────────────────

def test_extract_json_parses_clean_object():
    text = '{"a": 1, "b": "two"}'
    assert ai_engine._extract_json(text) == {"a": 1, "b": "two"}


def test_extract_json_strips_markdown_fences():
    text = '```json\n{"a": 1}\n```'
    assert ai_engine._extract_json(text) == {"a": 1}


def test_extract_json_skips_leading_non_dict_and_finds_real_object():
    """A greedy `\\{.*\\}` regex would happily match across '{"x": 1} some
    text {"a": 1, "b": 2}' and fail to parse. The decoder-based approach
    must find the actual valid dict, not just the first '{' character."""
    text = 'Here is a note: {malformed, then the real answer: {"a": 1, "b": 2}'
    assert ai_engine._extract_json(text) == {"a": 1, "b": 2}


def test_extract_json_rejects_non_dict_root():
    with pytest.raises(ValueError):
        ai_engine._extract_json("[1, 2, 3]")


def test_extract_json_raises_on_no_json():
    with pytest.raises(ValueError):
        ai_engine._extract_json("no json here at all")


# ── _fmt_pct / _fmt_money helpers ─────────────────────────────────────────

def test_fmt_pct_none_is_not_available():
    assert ai_engine._fmt_pct(None) == "N/A"


def test_fmt_pct_nan_and_inf_are_not_available():
    assert ai_engine._fmt_pct(float("nan")) == "N/A"
    assert ai_engine._fmt_pct(float("inf")) == "N/A"


def test_fmt_pct_formats_signed_value():
    assert ai_engine._fmt_pct(12.34) == "+12.3%"
    assert ai_engine._fmt_pct(-5.0) == "-5.0%"


def test_fmt_money_handles_malformed_amount():
    assert ai_engine._fmt_money("USD", "not-a-number") == "USD N/A"
    assert ai_engine._fmt_money("USD", 100) == "USD 100.00"


# ── analyse_cost_summary — happy path ─────────────────────────────────────

def test_analyse_cost_summary_happy_path(monkeypatch):
    monkeypatch.setattr(ai_engine, "_get_client", lambda: object())

    def fake_invoke(client, model_id, prompt, max_tokens=900):
        assert model_id == ai_engine.MODEL_ANALYSIS
        return json.dumps({
            "executive_summary": "Spend is up modestly this month.",
            "trend": "increasing",
            "key_cost_drivers": ["EC2 usage growth", "New RDS instances", "Marketplace spend", "extra ignored driver"],
            "recommendations": ["Buy Savings Plans", "Tag untagged resources", "Set budget alerts", "Review idle EBS", "extra ignored rec"],
            "bottom_line": "Approve as-is.",
        })

    monkeypatch.setattr(ai_engine, "_invoke", fake_invoke)
    result = ai_engine.analyse_cost_summary(_cost_summary())

    assert result["model_used"] == ai_engine.MODEL_ANALYSIS
    assert result["trend"] == "increasing"
    assert result["executive_summary"] == "Spend is up modestly this month."
    # Capped at 3 drivers / 4 recommendations even though the model
    # returned more.
    assert len(result["key_cost_drivers"]) == 3
    assert len(result["recommendations"]) == 4
    assert result["as_of"] == "2026-08-01T00:00:00Z"
    assert result["account_count"] == 3
    assert result["error_count"] == 0
    assert "generated_at" in result


# ── Data-quality: unavailable percentages, error/stale warnings ──────────

def test_analyse_cost_summary_handles_none_percentages_without_literal_none(monkeypatch):
    """Regression guard: the prompt (and therefore anything derived from
    it) must never contain the literal text 'None%' for an unavailable
    percentage — this test inspects the prompt actually sent to the model."""
    monkeypatch.setattr(ai_engine, "_get_client", lambda: object())
    captured_prompt = {}

    def fake_invoke(client, model_id, prompt, max_tokens=900):
        captured_prompt["text"] = prompt
        return json.dumps({
            "executive_summary": "Stable spend.",
            "trend": "stable",
            "key_cost_drivers": [],
            "recommendations": [],
            "bottom_line": "OK.",
        })

    monkeypatch.setattr(ai_engine, "_invoke", fake_invoke)
    summary = _cost_summary(mtd_vs_last_month_pct=None, last_month_vs_prior_pct=None)
    ai_engine.analyse_cost_summary(summary)

    assert "None%" not in captured_prompt["text"]
    assert "N/A" in captured_prompt["text"]


def test_analyse_cost_summary_includes_error_count_warning_in_prompt(monkeypatch):
    monkeypatch.setattr(ai_engine, "_get_client", lambda: object())
    captured_prompt = {}

    def fake_invoke(client, model_id, prompt, max_tokens=900):
        captured_prompt["text"] = prompt
        return json.dumps({"executive_summary": "x", "trend": "stable", "key_cost_drivers": [], "recommendations": [], "bottom_line": ""})

    monkeypatch.setattr(ai_engine, "_invoke", fake_invoke)
    summary = _cost_summary(error_count=2, account_count=5)
    result = ai_engine.analyse_cost_summary(summary)

    assert "2 of 5 account(s) failed" in captured_prompt["text"]
    assert result["error_count"] == 2


def test_analyse_cost_summary_flags_stale_fallback_in_prompt(monkeypatch):
    monkeypatch.setattr(ai_engine, "_get_client", lambda: object())
    captured_prompt = {}

    def fake_invoke(client, model_id, prompt, max_tokens=900):
        captured_prompt["text"] = prompt
        return json.dumps({"executive_summary": "x", "trend": "stable", "key_cost_drivers": [], "recommendations": [], "bottom_line": ""})

    monkeypatch.setattr(ai_engine, "_invoke", fake_invoke)
    summary = _cost_summary(stale_fallback=True)
    ai_engine.analyse_cost_summary(summary)

    assert "last known-good data" in captured_prompt["text"]


# ── Model fallback: invocation failure, parse failure, schema failure ────

def test_analyse_cost_summary_falls_back_on_invocation_error(monkeypatch):
    monkeypatch.setattr(ai_engine, "_get_client", lambda: object())
    calls = []

    def fake_invoke(client, model_id, prompt, max_tokens=900):
        calls.append(model_id)
        if len(calls) == 1:  # first attempt fails; the retry (same model) succeeds
            raise RuntimeError("bedrock unavailable")
        return json.dumps({"executive_summary": "Fallback worked.", "trend": "stable", "key_cost_drivers": [], "recommendations": [], "bottom_line": ""})

    monkeypatch.setattr(ai_engine, "_invoke", fake_invoke)
    result = ai_engine.analyse_cost_summary(_cost_summary())

    assert calls == [ai_engine.MODEL_ANALYSIS, ai_engine.MODEL_ANALYSIS_FALLBACK]
    assert result["model_used"] == ai_engine.MODEL_ANALYSIS_FALLBACK
    assert result["executive_summary"] == "Fallback worked."


def test_analyse_cost_summary_falls_back_on_unparseable_response(monkeypatch):
    """A model that returns garbage text (not just one that errors) must
    also trigger the fallback model, not just get a blank/garbage result."""
    monkeypatch.setattr(ai_engine, "_get_client", lambda: object())
    calls = []

    def fake_invoke(client, model_id, prompt, max_tokens=900):
        calls.append(model_id)
        if len(calls) == 1:  # first attempt fails; the retry (same model) succeeds
            return "I cannot help with that request."  # no JSON at all
        return json.dumps({"executive_summary": "Fallback worked.", "trend": "stable", "key_cost_drivers": [], "recommendations": [], "bottom_line": ""})

    monkeypatch.setattr(ai_engine, "_invoke", fake_invoke)
    result = ai_engine.analyse_cost_summary(_cost_summary())

    assert calls == [ai_engine.MODEL_ANALYSIS, ai_engine.MODEL_ANALYSIS_FALLBACK]
    assert result["model_used"] == ai_engine.MODEL_ANALYSIS_FALLBACK


def test_analyse_cost_summary_falls_back_on_missing_executive_summary(monkeypatch):
    """Valid JSON but missing the one truly required field must also be
    treated as a schema failure and trigger the fallback model."""
    monkeypatch.setattr(ai_engine, "_get_client", lambda: object())
    calls = []

    def fake_invoke(client, model_id, prompt, max_tokens=900):
        calls.append(model_id)
        if len(calls) == 1:  # first attempt fails; the retry (same model) succeeds
            return json.dumps({"trend": "stable", "key_cost_drivers": [], "recommendations": [], "bottom_line": ""})
        return json.dumps({"executive_summary": "Fallback worked.", "trend": "stable", "key_cost_drivers": [], "recommendations": [], "bottom_line": ""})

    monkeypatch.setattr(ai_engine, "_invoke", fake_invoke)
    result = ai_engine.analyse_cost_summary(_cost_summary())

    assert calls == [ai_engine.MODEL_ANALYSIS, ai_engine.MODEL_ANALYSIS_FALLBACK]
    assert result["model_used"] == ai_engine.MODEL_ANALYSIS_FALLBACK


def test_analyse_cost_summary_static_fallback_when_both_models_fail(monkeypatch):
    """When even the fallback model fails, the result must be a safe,
    clearly-labelled static summary — never a raw/garbage model
    output surfaced as the executive summary."""
    monkeypatch.setattr(ai_engine, "_get_client", lambda: object())

    def fake_invoke(client, model_id, prompt, max_tokens=900):
        raise RuntimeError("bedrock completely down")

    monkeypatch.setattr(ai_engine, "_invoke", fake_invoke)
    result = ai_engine.analyse_cost_summary(_cost_summary())

    assert result["model_used"] == "fallback-static"
    assert "temporarily unavailable" in result["executive_summary"]
    assert result["key_cost_drivers"] == []
    assert result["recommendations"] == []


def test_analyse_cost_summary_static_fallback_when_response_never_valid(monkeypatch):
    monkeypatch.setattr(ai_engine, "_get_client", lambda: object())

    def fake_invoke(client, model_id, prompt, max_tokens=900):
        return "not json, ever"

    monkeypatch.setattr(ai_engine, "_invoke", fake_invoke)
    result = ai_engine.analyse_cost_summary(_cost_summary())

    assert result["model_used"] == "fallback-static"
    assert result["bottom_line"] == "AI summary generation failed — review the cost table directly."
