"""
Model wiring in app/ai_engine.py after the move off Amazon Nova:
  * analysis/extraction → Claude Sonnet 5.5 through Converse (no temperature)
  * chat → GPT-6.1 Terra on bedrock-mantle with the web_search tool
  * every Bedrock call can run under an assumed cross-account role

No network and no real AWS calls: clients/HTTP are mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app import ai_engine


# ── model ids ───────────────────────────────────────────────────────────────

def test_model_ids():
    assert ai_engine.MODEL_ANALYSIS == "us.anthropic.claude-sonnet-5-5"
    assert ai_engine.MODEL_ANALYSIS_FALLBACK == ai_engine.MODEL_ANALYSIS
    assert ai_engine.MODEL_CHAT == "openai.gpt-6.1-terra"
    assert ai_engine.MODEL_PRIMARY == ai_engine.MODEL_ANALYSIS


# ── _invoke (Converse) ──────────────────────────────────────────────────────

def test_invoke_uses_converse_without_temperature():
    client = MagicMock()
    client.converse.return_value = {"output": {"message": {"content": [{"text": "hello"}]}}}

    assert ai_engine._invoke(client, ai_engine.MODEL_ANALYSIS, "prompt", max_tokens=321) == "hello"

    kwargs = client.converse.call_args.kwargs
    assert kwargs["modelId"] == "us.anthropic.claude-sonnet-5-5"
    assert kwargs["messages"] == [{"role": "user", "content": [{"text": "prompt"}]}]
    # Sonnet 5.5 rejects temperature; its hidden reasoning tokens share maxTokens with the
    # answer, so the limit carries headroom and reasoning effort is low.
    assert kwargs["inferenceConfig"] == {"maxTokens": 321 + ai_engine.CLAUDE_TOKEN_HEADROOM}
    assert kwargs["additionalModelRequestFields"] == {"output_config": {"effort": "low"}}
    client.invoke_model.assert_not_called()                  # Nova-style InvokeModel body is gone


def test_claude_params_leave_room_for_an_answer_bigger_than_the_hint():
    # Regression: production returned truncated JSON ("No JSON object found") because the
    # old 900-token limit was consumed by reasoning + answer (stopReason=max_tokens).
    params = ai_engine.claude_converse_params(900)
    assert params["inferenceConfig"]["maxTokens"] >= 900 + 2000
    assert "temperature" not in params["inferenceConfig"]


def test_invoke_warns_when_the_answer_was_cut_off(caplog):
    client = MagicMock()
    client.converse.return_value = {"stopReason": "max_tokens",
                                    "output": {"message": {"content": [{"text": '{"a":'}]}}}
    with caplog.at_level("WARNING", logger="app.ai_engine"):
        ai_engine._invoke(client, ai_engine.MODEL_ANALYSIS, "p")
    assert "token limit" in caplog.text


def test_invoke_skips_a_reasoning_block_before_the_text():
    client = MagicMock()
    client.converse.return_value = {"output": {"message": {"content": [
        {"reasoningContent": {"reasoningText": {"text": "thinking"}}},
        {"text": "answer"},
    ]}}}
    assert ai_engine._invoke(client, ai_engine.MODEL_ANALYSIS, "p") == "answer"


def test_invoke_raises_when_there_is_no_text():
    client = MagicMock()
    client.converse.return_value = {"output": {"message": {"content": []}}}
    with pytest.raises(ValueError):
        ai_engine._invoke(client, ai_engine.MODEL_ANALYSIS, "p")


# ── chat via bedrock-mantle ─────────────────────────────────────────────────

def test_converse_messages_become_responses_input():
    out = ai_engine._converse_to_responses_input([
        {"role": "user", "content": [{"text": "hi"}]},
        {"role": "assistant", "content": [{"text": "hello"}]},
        {"role": "user", "content": [{"text": "  "}]},        # blank → dropped
    ])
    assert out == [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]


def _responses_payload(text="Python 3.15 is current.", urls=("https://www.python.org/downloads/",)):
    return {
        "status": "completed",
        "output": [
            {"type": "reasoning", "summary": []},
            {"type": "web_search_call", "status": "completed"},
            {"type": "message", "content": [{
                "type": "output_text",
                "text": text,
                "annotations": [{"type": "url_citation", "url": u, "title": "t"} for u in urls]
                               + [{"type": "other"}],
            }]},
        ],
    }


def test_chat_sends_a_web_search_request_to_mantle_and_parses_citations(monkeypatch):
    sent = {}

    def fake_post(body, timeout=120.0):
        sent.update(body)
        return _responses_payload(urls=("https://www.python.org/downloads/", "https://docs.python.org/3/"))

    monkeypatch.setattr(ai_engine, "_mantle_post", fake_post)

    answer, citations = ai_engine.chat_with_grounding(
        object(),   # client is accepted but unused
        messages=[{"role": "user", "content": [{"text": "latest python?"}]}],
        system_prompt="be brief",
        max_tokens=1200,
    )

    assert answer == "Python 3.15 is current."
    assert citations == [
        {"url": "https://www.python.org/downloads/", "domain": "www.python.org"},
        {"url": "https://docs.python.org/3/", "domain": "docs.python.org"},
    ]
    assert sent["model"] == "openai.gpt-6.1-terra"
    assert sent["instructions"] == "be brief"
    assert sent["input"] == [{"role": "user", "content": "latest python?"}]
    # web search must stay inside the AWS boundary, and nothing is stored server-side
    assert sent["tools"] == [{"type": "web_search", "external_web_access": False}]
    assert sent["store"] is False
    assert sent["max_output_tokens"] > 1200            # headroom for reasoning tokens
    assert sent["reasoning"] == {"effort": "medium"}


def test_chat_without_thinking_uses_low_reasoning_effort(monkeypatch):
    sent = {}
    monkeypatch.setattr(ai_engine, "_mantle_post", lambda body, timeout=120.0: sent.update(body) or _responses_payload())
    ai_engine.chat_with_grounding(object(), [{"role": "user", "content": [{"text": "x"}]}], "s", enable_thinking=False)
    assert sent["reasoning"] == {"effort": "low"}


def test_chat_strips_leaked_thinking_tags(monkeypatch):
    monkeypatch.setattr(ai_engine, "_mantle_post",
                        lambda body, timeout=120.0: _responses_payload(text="<thinking>hmm</thinking>Final.", urls=()))
    answer, citations = ai_engine.chat_with_grounding(object(), [{"role": "user", "content": [{"text": "x"}]}], "s")
    assert answer == "Final." and citations == []


def test_chat_with_no_answer_raises(monkeypatch):
    monkeypatch.setattr(ai_engine, "_mantle_post", lambda body, timeout=120.0: {
        "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
        "output": [{"type": "reasoning"}],
    })
    with pytest.raises(RuntimeError, match="max_output_tokens"):
        ai_engine.chat_with_grounding(object(), [{"role": "user", "content": [{"text": "x"}]}], "s")


def test_mantle_post_signs_the_request_for_bedrock_mantle(monkeypatch):
    creds = MagicMock()
    creds.get_frozen_credentials.return_value = MagicMock(
        access_key="AKIAEXAMPLE", secret_key="secret", token="tok")
    session = MagicMock()
    session.get_credentials.return_value = creds
    monkeypatch.setattr(ai_engine, "_get_session", lambda: session)

    captured = {}

    def fake_httpx_post(url, content=None, headers=None, timeout=None):
        captured.update(url=url, headers=headers, content=content)
        return MagicMock(status_code=200, json=lambda: {"ok": True})

    monkeypatch.setattr(ai_engine.httpx, "post", fake_httpx_post)

    assert ai_engine._mantle_post({"model": "m"}) == {"ok": True}
    assert captured["url"] == "https://bedrock-mantle.us-east-1.api.aws/openai/v1/responses"
    auth = captured["headers"]["Authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256") and "/us-east-1/bedrock-mantle/aws4_request" in auth
    assert captured["headers"]["X-Amz-Security-Token"] == "tok"


def test_mantle_error_is_raised_with_status(monkeypatch):
    session = MagicMock()
    session.get_credentials.return_value.get_frozen_credentials.return_value = MagicMock(
        access_key="a", secret_key="b", token=None)
    monkeypatch.setattr(ai_engine, "_get_session", lambda: session)
    monkeypatch.setattr(ai_engine.httpx, "post",
                        lambda *a, **k: MagicMock(status_code=403, text="AccessDenied"))
    with pytest.raises(RuntimeError, match="HTTP 403"):
        ai_engine._mantle_post({"model": "m"})


# ── cross-account session ───────────────────────────────────────────────────

@pytest.fixture(autouse=False)
def clean_session_cache():
    ai_engine._session_cache.clear()
    yield
    ai_engine._session_cache.clear()


def test_session_uses_default_chain_without_a_role(monkeypatch, clean_session_cache):
    monkeypatch.delenv("BEDROCK_ROLE_ARN", raising=False)
    s = ai_engine._get_session()
    assert s is ai_engine._get_session()          # cached
    assert s.region_name == "us-east-1"


def test_session_assumes_the_configured_role(monkeypatch, clean_session_cache):
    role = "arn:aws:iam::100000000005:role/JobPortal-Bedrock-Invoke"
    monkeypatch.setenv("BEDROCK_ROLE_ARN", role)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIASOURCE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "source-secret")
    monkeypatch.delenv("AWS_PROFILE", raising=False)

    seen = {}

    class FakeFetcher:
        def __init__(self, client_creator, source_credentials, role_arn, extra_args=None, **kw):
            seen.update(role_arn=role_arn, extra_args=extra_args)

        def fetch_credentials(self):
            return {"access_key": "ASIAASSUMED", "secret_key": "s", "token": "t",
                    "expiry_time": "2999-01-01T00:00:00+00:00"}

    monkeypatch.setattr("botocore.credentials.AssumeRoleCredentialFetcher", FakeFetcher)

    session = ai_engine._get_session()
    frozen = session.get_credentials().get_frozen_credentials()

    assert seen["role_arn"] == role
    assert seen["extra_args"]["RoleSessionName"] == "job-portal-bedrock"
    assert frozen.access_key == "ASIAASSUMED"
    # a different role ARN gets its own session
    monkeypatch.setenv("BEDROCK_ROLE_ARN", role + "-2")
    assert ai_engine._get_session() is not session
