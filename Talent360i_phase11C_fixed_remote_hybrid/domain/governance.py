"""Phase 9 governance primitives: CSRF, audit, agent runs and token budget.

This module is intentionally storage-agnostic. It provides the same contracts
that the persistent SQLAlchemy backend will later implement.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
from threading import Lock

DAILY_TOKEN_CAP = 500_000
AGENT_CAPS = {
    "A1": 50_000,
    "A2": 300_000,
    "A3": 75_000,
    "A4": 75_000,
    "A5": 50_000,
}

_AUDIT_EVENTS: list[dict] = []
_AGENT_RUNS: list[dict] = []
_LLM_CALLS: list[dict] = []
_BUDGET = defaultdict(lambda: {"consumed": 0, "cap": DAILY_TOKEN_CAP})
_LOCK = Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_valid(expected: str | None, supplied: str | None) -> bool:
    if not expected or not supplied:
        return False
    return secrets.compare_digest(expected, supplied)


def sanitise_text(value: str | None, limit: int = 2000) -> str:
    """Remove control characters and bound user-controlled text."""
    text = str(value or "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def mask_value(key: str, value: object) -> object:
    if value is None:
        return None
    lowered = key.lower()
    if any(word in lowered for word in ("password", "token", "secret", "authorization", "cookie")):
        return "[masked]"
    if any(word in lowered for word in ("email",)):
        text = str(value)
        if "@" in text:
            local, domain = text.split("@", 1)
            return (local[:1] + "***@" + domain) if local else "***@" + domain
    return sanitise_text(str(value), 500)


def masked_fields(fields: dict | None) -> dict:
    return {str(k): mask_value(str(k), v) for k, v in (fields or {}).items()}


_AUDIT_FILE = Path(os.getenv("TALENT360_LOG_DIR", Path(__file__).resolve().parents[2] / "logs")) / "portal_audit.jsonl"


def _persist_audit(event: dict) -> None:
    """Append-only JSONL copy so the audit trail survives a restart (guideline I: retained logs)."""
    try:
        _AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_AUDIT_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass  # never let logging take a request down


def record_audit(actor_id: str, actor_role: str, action: str, resource: str,
                 outcome: str = "success", details: dict | None = None) -> dict:
    event = {
        "id": f"AUD-{len(_AUDIT_EVENTS)+1:05d}",
        "timestamp": now_iso(),
        "actor_id": actor_id,
        "actor_role": actor_role,
        "action": sanitise_text(action, 120),
        "resource": sanitise_text(resource, 180),
        "outcome": sanitise_text(outcome, 80),
        "details": masked_fields(details),
    }
    with _LOCK:
        _AUDIT_EVENTS.append(event)
        _persist_audit(event)
    return deepcopy(event)


def get_audit_events(limit: int = 100) -> list[dict]:
    with _LOCK:
        return deepcopy(_AUDIT_EVENTS[-limit:][::-1])


def prompt_hash(prompt: str) -> str:
    return sha256(prompt.encode("utf-8")).hexdigest()


def reserve_budget(agent_id: str, estimated_tokens: int, day: str | None = None) -> dict:
    """Atomically enforce both the daily cap and per-agent cap."""
    if agent_id not in AGENT_CAPS:
        raise ValueError(f"Unknown agent: {agent_id}")
    day_key = day or datetime.now(timezone.utc).date().isoformat()
    key = f"{day_key}:{agent_id}"
    daily_key = f"{day_key}:__daily__"
    amount = max(0, int(estimated_tokens))
    with _LOCK:
        agent_row = _BUDGET[key]
        agent_row["cap"] = AGENT_CAPS[agent_id]
        daily_row = _BUDGET[daily_key]
        daily_row["cap"] = DAILY_TOKEN_CAP
        if agent_row["consumed"] + amount > agent_row["cap"]:
            raise RuntimeError(f"{agent_id} daily budget would be exceeded")
        if daily_row["consumed"] + amount > DAILY_TOKEN_CAP:
            raise RuntimeError("Global daily token budget would be exceeded")
        agent_row["consumed"] += amount
        daily_row["consumed"] += amount
        return {"date": day_key, "agent_id": agent_id, "reserved": amount,
                "agent_consumed": agent_row["consumed"], "agent_cap": agent_row["cap"],
                "daily_consumed": daily_row["consumed"], "daily_cap": DAILY_TOKEN_CAP}


def record_agent_run(agent_id: str, stage: str, status: str = "completed",
                     estimated_tokens: int = 0, metadata: dict | None = None) -> dict:
    budget = reserve_budget(agent_id, estimated_tokens)
    row = {
        "id": f"RUN-{len(_AGENT_RUNS)+1:05d}",
        "timestamp": now_iso(),
        "agent_id": agent_id,
        "stage": sanitise_text(stage, 120),
        "status": sanitise_text(status, 60),
        "estimated_tokens": int(estimated_tokens),
        "budget": budget,
        "metadata": masked_fields(metadata),
    }
    with _LOCK:
        _AGENT_RUNS.append(row)
    return deepcopy(row)


def record_llm_call(agent_id: str, prompt: str, estimated_tokens: int,
                    cached: bool = False, model: str = "company-cis-pending") -> dict:
    # Local phase records the intended call contract; no external endpoint is called.
    row = {
        "id": f"LLM-{len(_LLM_CALLS)+1:05d}",
        "timestamp": now_iso(),
        "agent_id": agent_id,
        "model": sanitise_text(model, 100),
        "prompt_hash": prompt_hash(prompt),
        "estimated_tokens": int(estimated_tokens),
        "cached": bool(cached),
    }
    with _LOCK:
        _LLM_CALLS.append(row)
    return deepcopy(row)


def get_agent_runs(limit: int = 100) -> list[dict]:
    with _LOCK:
        return deepcopy(_AGENT_RUNS[-limit:][::-1])


def get_llm_calls(limit: int = 100) -> list[dict]:
    with _LOCK:
        return deepcopy(_LLM_CALLS[-limit:][::-1])


def get_budget_snapshot() -> list[dict]:
    with _LOCK:
        rows = []
        for key, row in _BUDGET.items():
            if key.endswith(":__daily__"):
                continue
            day, agent = key.split(":", 1)
            rows.append({"date": day, "agent_id": agent, **row})
        rows.sort(key=lambda x: (x["date"], x["agent_id"]), reverse=True)
        return deepcopy(rows)


def governance_snapshot() -> dict:
    return {
        "audit_events": get_audit_events(),
        "agent_runs": get_agent_runs(),
        "llm_calls": get_llm_calls(),
        "budget": get_budget_snapshot(),
        "daily_token_cap": DAILY_TOKEN_CAP,
        "agent_caps": deepcopy(AGENT_CAPS),
    }
