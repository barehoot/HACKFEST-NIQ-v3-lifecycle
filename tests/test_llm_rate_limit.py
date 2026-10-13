"""
The LLM client waits out a provider's rate limit instead of failing the agent.

A workplace simulation's follow-up probes can use most of a minute's token budget
(Groq's free tier: 8,000 tokens per minute) right before Agent 4 writes the
evidence report. The provider's 429 says how long to wait; call_llm_json waits
that long and retries, and still fails fast on a long wait (a daily cap).
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))

import llm_client  # noqa: E402

GROQ_429 = ("Groq call failed (model=openai/gpt-oss-120b): Error code: 429 - {'error': {'message': 'Rate limit "
            "reached for model on tokens per minute (TPM): Limit 8000, Used 7599, Requested 3094. Please try again "
            "in 20.1975s.', 'type': 'tokens', 'code': 'rate_limit_exceeded'}}")


def test_the_wait_comes_from_the_providers_message():
    assert llm_client._rate_limit_wait(RuntimeError(GROQ_429)) == pytest.approx(20.1975)
    assert llm_client._rate_limit_wait(RuntimeError("Rate limit reached. Please try again in 1m30s.")) == 90
    assert llm_client._rate_limit_wait(RuntimeError("Error code: 429 - slow down")) == 5.0  # no advice: a short default
    assert llm_client._rate_limit_wait(RuntimeError("Groq call failed: Error code: 401 - invalid API key")) is None


@pytest.fixture
def provider(monkeypatch, tmp_path):
    calls, sleeps = [], []
    monkeypatch.setattr(llm_client.time, "sleep", sleeps.append)
    monkeypatch.setattr(llm_client.security, "LOG_DIR", tmp_path)  # keep test calls out of the real audit log

    def install(*outcomes):
        def fake(*args):
            outcome = outcomes[min(len(calls), len(outcomes) - 1)]
            calls.append(args)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        monkeypatch.setitem(llm_client._PROVIDERS, llm_client.LLM_PROVIDER, fake)
        return calls, sleeps
    return install


def test_a_rate_limited_call_waits_then_succeeds(provider):
    calls, sleeps = provider(RuntimeError(GROQ_429), '{"ok": true}')
    assert llm_client.call_llm_json("Return JSON.", "hello", agent="test") == {"ok": True}
    assert len(calls) == 2 and sleeps == [pytest.approx(20.6975)]


def test_retries_are_bounded(provider):
    calls, sleeps = provider(RuntimeError(GROQ_429))
    with pytest.raises(RuntimeError, match="429"):
        llm_client.call_llm_json("Return JSON.", "hello", agent="test")
    assert len(calls) == llm_client.LLM_RATE_LIMIT_RETRIES + 1 and len(sleeps) == llm_client.LLM_RATE_LIMIT_RETRIES


def test_a_long_wait_or_another_error_fails_fast(provider):
    calls, sleeps = provider(RuntimeError("Rate limit reached for requests per day. Please try again in 7m12s."))
    with pytest.raises(RuntimeError):
        llm_client.call_llm_json("Return JSON.", "hello", agent="test")
    assert len(calls) == 1 and sleeps == []

    calls, sleeps = provider(RuntimeError("Groq call failed: Error code: 401 - invalid API key"))
    with pytest.raises(RuntimeError, match="401"):
        llm_client.call_llm_json("Return JSON.", "hello", agent="test")
    assert sleeps == []
