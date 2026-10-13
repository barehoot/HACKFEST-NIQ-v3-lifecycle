"""Deterministic validation for A5 development-plan proposals."""
from __future__ import annotations

from copy import deepcopy

MAX_STEPS = 8


def validate_proposal(proposal: dict) -> dict:
    errors = []
    steps = proposal.get("steps") or []
    if len(steps) > MAX_STEPS:
        errors.append("Plan exceeds the maximum number of steps.")
    if proposal.get("status") == "Proposed" and not steps:
        errors.append("A proposed plan must contain at least one step.")
    required = {"skill", "framework", "title", "descriptor_clause", "training_link", "sop_reference"}
    for index, step in enumerate(steps, start=1):
        missing = sorted(required - set(step))
        if missing:
            errors.append(f"Step {index} missing: {', '.join(missing)}")
        if not str(step.get("descriptor_clause", "")).strip():
            errors.append(f"Step {index} has no descriptor clause.")
    return {"valid": not errors, "errors": errors, "proposal": deepcopy(proposal)}


def gap_severity(gap: int) -> str:
    if gap >= 2:
        return "High"
    if gap == 1:
        return "Medium"
    return "Low"
