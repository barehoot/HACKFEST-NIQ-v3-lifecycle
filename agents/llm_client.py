"""
llm_client.py

Sub-step 17 (Phase 5): the one client module that owns every LLM call.

The HLD's cross-cutting constraints require all model calls to go through
a single client module, so the CIS-only endpoint constraint, per-agent
token budgets, and prompt-hash caching can all be enforced in one place
instead of scattered across every agent (talent360i-hld-lld-v2 (2).html,
system context section).

Provider is selected purely by the LLM_PROVIDER env var:
    - "groq" (default) -- ChatGroq, for personal-machine development.
    - "cis"             -- the company's internal Azure AI Inference
                            gateway ("CIS LLM"), for the company machine.

Every other agent still only ever calls call_llm_json() below -- never a
provider SDK directly -- so switching machines/providers means changing
.env only, never any code (confirmed: the only caller anywhere in this
repo is agents/agent2_question.py, and it never passes model= or
temperature=, so this file is free to change internally without touching
any other file).
"""

import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
import security  # noqa: E402

load_dotenv()

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "cis").strip().lower()
TALENT360_ENV = os.getenv("TALENT360_ENV", "development").strip().lower()
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "90"))
# A rate-limit refusal (HTTP 429, e.g. Groq's tokens-per-minute cap) says how long to wait.
# Waiting that long and retrying beats failing the agent: a workplace simulation's probes
# can use most of a minute's budget right before the evidence report needs it. A wait
# longer than LLM_RATE_LIMIT_MAX_WAIT (a daily cap, say) still fails fast.
LLM_RATE_LIMIT_RETRIES = int(os.getenv("LLM_RATE_LIMIT_RETRIES", "3"))
LLM_RATE_LIMIT_MAX_WAIT = float(os.getenv("LLM_RATE_LIMIT_MAX_WAIT", "30"))
_RETRY_IN = re.compile(r"(?:try again|retry) in (?:(\d+)m)?([\d.]+)s", re.IGNORECASE)
# Off by default: prompts can contain resume PII. Turn on (redacted) only
# for prompt-injection forensics per guideline G ("prompts and responses
# must be logged and monitored").
LOG_FULL_TEXT = os.getenv("LLM_LOG_FULL_TEXT", "false").strip().lower() == "true"

GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

CIS_ENDPOINT = os.getenv("CIS_LLM_ENDPOINT", "https://llm-api-cis.azure-intlsd-np.nielsencsp.net/")
CIS_MODEL = os.getenv("CIS_MODEL", "hack-fest-gpt-5.6-luna")
CIS_API_VERSION = os.getenv("CIS_API_VERSION", "2025-03-01-preview")

_DEFAULT_MODELS = {"groq": GROQ_MODEL, "cis": CIS_MODEL}

_client_cache = {}
_NO_TEMPERATURE_MODELS = set()


class _TemperatureUnsupported(Exception):
    pass


def _call_groq(system_prompt, user_prompt, model, temperature):
    from langchain_groq import ChatGroq
    from langchain_core.messages import SystemMessage, HumanMessage

    if not os.getenv("GROQ_API_KEY"):
        raise RuntimeError(
            "GROQ_API_KEY is not set (LLM_PROVIDER=groq). Add it to the .env "
            "file at the project root (GROQ_API_KEY=your_key_here). Get a "
            "free key at https://console.groq.com/keys."
        )

    key = ("groq", model, temperature)
    if key not in _client_cache:
        _client_cache[key] = ChatGroq(
            # One quick SDK retry for a dropped connection or a 5xx; call_llm_json waits out rate limits.
            model=model, temperature=temperature, timeout=LLM_TIMEOUT_SECONDS, max_retries=1
        )
    llm = _client_cache[key]

    try:
        response = llm.invoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
        )
    except Exception as exc:
        # Unlike _call_cis, this previously let groq's own exception types
        # (RateLimitError, APIError, ...) propagate raw -- FastAPI then
        # returned its default plain-text 500 page instead of JSON, which
        # broke every caller's res.json() on the frontend. Wrapping in
        # RuntimeError here means every endpoint's existing
        # "except RuntimeError" handler catches it and returns a proper
        # JSON error, same as the CIS provider already does.
        raise RuntimeError(f"Groq call failed (model={model}): {exc}") from exc

    return response.content


def _call_cis(system_prompt, user_prompt, model, temperature):
    from azure.ai.inference import ChatCompletionsClient
    from azure.ai.inference.models import SystemMessage, UserMessage
    from azure.core.credentials import AzureKeyCredential

    api_key = os.getenv("CIS_API_KEY")
    if not api_key:
        raise RuntimeError(
            "CIS_API_KEY is not set (LLM_PROVIDER=cis). Add it to the .env "
            "file exactly as issued by your company, including the "
            "'Bearer ' prefix (CIS_API_KEY=Bearer sk-...)."
        )

    # TLS 1.2+ / HTTPS only for every model call (guideline B.1).
    if not CIS_ENDPOINT.lower().startswith("https://"):
        raise RuntimeError("CIS_LLM_ENDPOINT must use https://.")

    key = ("cis", CIS_ENDPOINT)
    if key not in _client_cache:
        _client_cache[key] = ChatCompletionsClient(
            endpoint=CIS_ENDPOINT,
            credential=AzureKeyCredential(api_key),
            api_version=CIS_API_VERSION,
            connection_timeout=15,
            read_timeout=LLM_TIMEOUT_SECONDS,
        )
    client = _client_cache[key]
    messages = [SystemMessage(content=system_prompt), UserMessage(content=user_prompt)]

    try:
        try:
            # GPT-5-family deployments only accept the default temperature and
            # reject any other value with a 400. Retry once without it and
            # remember that for this model, so later calls skip the wasted request.
            if model in _NO_TEMPERATURE_MODELS:
                raise _TemperatureUnsupported
            response = client.complete(messages=messages, model=model, temperature=temperature,
                                       headers={"Authorization": api_key})
        except Exception as exc:
            if not isinstance(exc, _TemperatureUnsupported) and "temperature" not in str(exc).lower():
                raise
            _NO_TEMPERATURE_MODELS.add(model)
            response = client.complete(messages=messages, model=model, headers={"Authorization": api_key})
    except Exception as exc:
        # The CIS endpoint is an internal company address -- on a machine
        # off the company network/VPN this times out rather than
        # connection-refuses, which otherwise surfaces as a long,
        # confusing azure-core traceback. Surface it as one clear line.
        raise RuntimeError(
            f"CIS endpoint call failed ({CIS_ENDPOINT}, model={model}): {exc}. "
            "If you're not on the company network/VPN, that's expected."
        ) from exc

    return response.choices[0].message.content


_PROVIDERS = {"groq": _call_groq, "cis": _call_cis}


def _rate_limit_wait(exc):
    """Seconds the provider asked us to wait when exc is a rate-limit refusal, else None."""
    cause = exc.__cause__ or exc
    response = getattr(cause, "response", None)
    status = getattr(cause, "status_code", None) or getattr(response, "status_code", None)
    text = str(exc)
    if status != 429 and "rate limit" not in text.lower() and "error code: 429" not in text.lower():
        return None
    try:
        return float((getattr(response, "headers", None) or {}).get("retry-after"))
    except (TypeError, ValueError):
        pass
    match = _RETRY_IN.search(text)
    return int(match.group(1) or 0) * 60 + float(match.group(2)) if match else 5.0


def _strip_code_fences(text):
    # Models frequently wrap JSON in ```json ... ``` even when told not to.
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    return match.group(1).strip() if match else text.strip()


def _check_provider_allowed():
    if LLM_PROVIDER not in _PROVIDERS:
        raise RuntimeError(
            f"Unknown LLM_PROVIDER '{LLM_PROVIDER}'. Set it to 'groq' or "
            "'cis' in the .env file (or unset it to default to 'groq')."
        )
    # NIQ GenAI guideline E: only approved model-hosting providers (Azure AI
    # Services -> the CIS gateway) may be used with real data. Groq is a
    # personal-machine development convenience only.
    if TALENT360_ENV in {"production", "prod", "staging"} and LLM_PROVIDER != "cis":
        raise RuntimeError(
            f"LLM_PROVIDER='{LLM_PROVIDER}' is not an approved provider for TALENT360_ENV={TALENT360_ENV}. "
            "Use 'cis' (Azure AI Services)."
        )


def call_llm_json(system_prompt, user_prompt, model=None, temperature=0.3, agent="unknown"):
    """
    Sends one system+user message pair to whichever provider LLM_PROVIDER
    selects, and parses the response as JSON.

    Security controls applied here for every agent (single choke point):
      - the trailing security reminder is appended AFTER all external input
      - the prompt is capped at MAX_PROMPT_CHARS (context-window limit)
      - each call is logged (hash, size, injection indicators, outcome) --
        raw prompt text is only logged when LLM_LOG_FULL_TEXT=true, redacted
      - model output is parsed as JSON, must be an object, and every string
        in it is sanitised before any caller or browser sees it

    Raises ValueError if the model didn't return a valid JSON object,
    rather than silently returning None or a partial dict -- callers need
    to know generation failed, not guess from an empty result.
    """
    _check_provider_allowed()

    user_prompt = str(user_prompt) + security.GUARD_SUFFIX
    if len(system_prompt) + len(user_prompt) > security.MAX_PROMPT_CHARS:
        security.audit(
            "llm_call", actor=agent, outcome="rejected_too_long", stream="llm",
            details={"prompt_chars": len(system_prompt) + len(user_prompt)},
        )
        raise RuntimeError("Prompt exceeds the maximum allowed size; request rejected.")

    effective_model = model or _DEFAULT_MODELS[LLM_PROVIDER]
    log_details = {
        "provider": LLM_PROVIDER,
        "model": effective_model,
        "prompt_sha256": security.sha256_hex(system_prompt + user_prompt),
        "prompt_chars": len(system_prompt) + len(user_prompt),
        "injection_indicators": security.detect_injection(user_prompt),
    }
    if LOG_FULL_TEXT:
        log_details["prompt"] = security.redact(user_prompt)

    attempt = 0
    while True:
        try:
            raw = _PROVIDERS[LLM_PROVIDER](system_prompt, user_prompt, effective_model, temperature)
            break
        except RuntimeError as exc:
            wait = _rate_limit_wait(exc)
            if wait is None or attempt >= LLM_RATE_LIMIT_RETRIES or wait > LLM_RATE_LIMIT_MAX_WAIT:
                security.audit("llm_call", actor=agent, outcome="provider_error", stream="llm",
                               details={**log_details, "error": security.redact(exc)})
                raise
            attempt += 1
            security.audit("llm_call", actor=agent, outcome="rate_limited_retrying", stream="llm",
                           details={**log_details, "wait_seconds": round(wait, 1), "attempt": attempt})
            time.sleep(wait + 0.5)

    log_details["response_chars"] = len(raw or "")
    if LOG_FULL_TEXT:
        log_details["response"] = security.redact(raw or "")

    try:
        parsed = json.loads(_strip_code_fences(raw))
    except json.JSONDecodeError as exc:
        security.audit("llm_call", actor=agent, outcome="invalid_json", stream="llm", details=log_details)
        raise ValueError("LLM did not return valid JSON.") from exc

    if not isinstance(parsed, dict):
        security.audit("llm_call", actor=agent, outcome="not_an_object", stream="llm", details=log_details)
        raise ValueError("LLM response was not a JSON object.")

    security.audit("llm_call", actor=agent, outcome="success", stream="llm", details=log_details)
    return security.sanitize_output(parsed)


def print_llm_smoke_test():
    """
    Test helper: one trivial call+parse round trip, to confirm the active
    provider's key works and JSON parsing works, before any agent tries
    to use this client for real generation.
    """
    print(f"LLM_PROVIDER={LLM_PROVIDER}")
    try:
        result = call_llm_json(
            system_prompt="Respond with ONLY a JSON object, no prose, no markdown fences.",
            user_prompt='Return this exact JSON object: {"ok": true, "message": "llm_client is working"}',
        )
        print(f"Smoke test result: {result}")
    except (RuntimeError, ValueError) as exc:
        print(f"Smoke test failed: {exc}")


if __name__ == "__main__":
    print_llm_smoke_test()
