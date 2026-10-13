"""Deterministic local stand-in for the HLD A4 Evidence Agent.

A4 elicits and evaluates evidence for behavioural competencies. It may ask up
 to three probes, summarise the employee's own words, identify missing evidence,
and estimate sufficiency/confidence. It never assigns or recommends a level.
"""
from __future__ import annotations

import re
from copy import deepcopy

MAX_PROBES = 3
BEHAVIOURAL_COMPETENCIES = [
    {"id": "BHV_001", "name": "Ownership & Accountability", "indicator": "Takes ownership, follows through on commitments and makes risks visible."},
    {"id": "BHV_002", "name": "Stakeholder Communication", "indicator": "Adapts communication to stakeholder needs and drives a clear outcome."},
    {"id": "BHV_003", "name": "Collaboration", "indicator": "Works across teams, resolves dependencies and contributes to shared outcomes."},
    {"id": "BHV_004", "name": "Adaptability", "indicator": "Adjusts approach when priorities, processes or circumstances change."},
    {"id": "BHV_005", "name": "Problem Solving", "indicator": "Frames problems, investigates causes and applies practical solutions."},
    {"id": "BHV_006", "name": "Customer Focus", "indicator": "Understands customer impact and balances service with process and control requirements."},
]

PROMPTS = {
    "BHV_001": "Describe a situation where you personally owned an issue through to closure. What did you do and what was the outcome?",
    "BHV_002": "Describe a recent situation where you adapted communication for different stakeholders. Include your actions and the outcome.",
    "BHV_003": "Tell us about a cross-team dependency you helped resolve. What did you contribute and what changed as a result?",
    "BHV_004": "Describe a time when a priority, process or requirement changed unexpectedly. How did you adapt?",
    "BHV_005": "Describe a difficult operational problem you solved. Explain how you investigated the cause and selected your response.",
    "BHV_006": "Describe a situation where you had to balance customer needs with process, risk or control requirements.",
}

PROBE_TEMPLATES = {
    "situation": "What was the specific situation, scope or business impact?",
    "action": "What did you personally do, and which decisions did you make?",
    "outcome": "What was the measurable or observable outcome, and what did you learn?",
}

KEYWORDS = {
    "situation": ["situation", "context", "issue", "problem", "exception", "impact"],
    "action": ["i ", "reviewed", "investigated", "checked", "validated", "communicated", "escalated", "resolved", "reconciled", "implemented", "coordinated"],
    "reasoning": ["because", "root cause", "risk", "control", "policy", "procedure", "trade-off", "reason", "decided"],
    "outcome": ["outcome", "result", "closed", "reduced", "prevented", "completed", "improved", "saved", "resolved", "without delay"],
}


def sanitise_employee_text(text: str, limit: int = 3000) -> str:
    """Remove obvious prompt-injection/control strings before an eventual LLM call.

    This is a deliberately conservative demo control. It does not execute or
    interpret user text as instructions.
    """
    clean = " ".join((text or "").replace("\x00", " ").split())[:limit]
    blocked = ["ignore previous instructions", "system prompt", "developer message", "jailbreak"]
    for phrase in blocked:
        clean = re.sub(re.escape(phrase), "[redacted instruction-like text]", clean, flags=re.I)
    return clean


def _hits(text: str, bucket: str) -> int:
    lower = text.lower()
    return sum(1 for keyword in KEYWORDS[bucket] if keyword in lower)


def assess_evidence(competency: dict, evidence: str, probes: list[dict] | None = None) -> dict:
    clean = sanitise_employee_text(evidence)
    probes = deepcopy(probes or [])
    text = clean + " " + " ".join(sanitise_employee_text(p.get("answer", ""), 1500) for p in probes)
    coverage = {bucket: _hits(text, bucket) > 0 for bucket in ("situation", "action", "reasoning", "outcome")}
    covered = sum(coverage.values())
    confidence = round(min(0.98, 0.42 + covered * 0.10 + min(len(probes), MAX_PROBES) * 0.06 + (0.08 if len(text) >= 220 else 0)), 2)
    missing = [bucket for bucket, present in coverage.items() if not present]
    sufficient = covered >= 3 and len(text) >= 80
    next_probe = None
    if not sufficient and len(probes) < MAX_PROBES:
        focus = missing[0] if missing else "outcome"
        next_probe = PROBE_TEMPLATES.get(focus, PROBE_TEMPLATES["action"])
    quoted = clean[:260]
    return {
        "competency_id": competency["id"],
        "competency": competency["name"],
        "indicator": competency["indicator"],
        "coverage": coverage,
        "covered_dimensions": covered,
        "missing": missing,
        "sufficient": sufficient,
        "confidence": confidence,
        "probe_count": len(probes),
        "next_probe": next_probe,
        "summary": f'Employee evidence: “{quoted}”' if quoted else "No evidence submitted yet.",
        "evaluation": "Evidence covers the key behavioural indicators sufficiently for manager review." if sufficient else "More specific evidence is needed before manager review.",
        "decision": "No level decision made by A4. Manager confirmation is required.",
    }
