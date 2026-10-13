"""Phase 5 deterministic stand-in for the HLD A3 Assessment Agent.

A3 may propose descriptive-answer scoring, confidence and escalation. It never
sets an employee proficiency level. The deterministic domain layer remains the
only place that calculates the level from the assessment result.
"""
from __future__ import annotations

import re
from copy import deepcopy

RUBRIC_DIMENSIONS = ["situation", "action", "control_or_reasoning", "outcome"]

KEYWORDS = {
    "situation": ["situation", "context", "issue", "problem", "variance", "exception", "stakeholder"],
    "action": ["action", "reviewed", "investigated", "checked", "validated", "communicated", "escalated", "reconciled", "resolved"],
    "control_or_reasoning": ["control", "approval", "evidence", "procedure", "policy", "risk", "root cause", "maker-checker", "three-way"],
    "outcome": ["outcome", "result", "resolved", "closed", "reduced", "prevented", "completed", "improved", "without delay"],
}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]


def _dimension_score(text: str, dimension: str) -> int:
    lower = text.lower()
    hits = sum(1 for keyword in KEYWORDS[dimension] if keyword in lower)
    if hits >= 2:
        return 2
    if hits == 1:
        return 1
    return 0


def score_descriptive_answer(question: dict, answer: str) -> dict:
    """Produce an A3 proposal for one descriptive answer.

    This intentionally uses deterministic heuristics for the local MVP. The
    production implementation will replace this function with a validated
    company-LLM proposal while retaining the same output contract.
    """
    clean = " ".join((answer or "").split())[:5000]
    sentences = _sentences(clean)
    dimensions = {name: _dimension_score(clean, name) for name in RUBRIC_DIMENSIONS}
    total = sum(dimensions.values())
    max_score = len(RUBRIC_DIMENSIONS) * 2
    score_percent = round(total / max_score * 100) if max_score else 0
    confidence = round(min(0.99, 0.45 + min(len(sentences), 5) * 0.08 + (sum(v > 0 for v in dimensions.values()) * 0.08)), 2)
    borderline = confidence < 0.70 or score_percent < 60 or any(value == 0 for value in dimensions.values())
    citations = []
    for dimension, value in dimensions.items():
        if value:
            source = sentences[min(RUBRIC_DIMENSIONS.index(dimension), max(0, len(sentences) - 1))] if sentences else clean
            citations.append({"dimension": dimension, "evidence": source[:220]})

    return {
        "question_id": question["number"],
        "question": question["text"],
        "skill": question["skill"],
        "framework": question.get("framework", "FinOps"),
        "rubric": deepcopy(question.get("rubric", RUBRIC_DIMENSIONS)),
        "dimension_scores": dimensions,
        "score_percent": score_percent,
        "confidence": confidence,
        "borderline": borderline,
        "citations": citations,
        "status": "Escalated to SME" if borderline else "A3 Proposal",
        "note": "A3 proposal only; employee level is not determined by this agent.",
    }


def scenario_score(question: dict, answer: str | None) -> dict:
    """Deterministic scenario-choice scoring; scenarios do not invoke A3."""
    selected = answer if answer in {"A", "B", "C", "D"} else None
    correct = question.get("correct")
    return {
        "question_id": question["number"],
        "selected": selected,
        "correct": correct,
        "is_correct": bool(selected and correct and selected == correct),
        "score_percent": 100 if selected and correct and selected == correct else 0,
    }
