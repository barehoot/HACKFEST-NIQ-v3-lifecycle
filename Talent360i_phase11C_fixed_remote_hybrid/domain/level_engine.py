"""Deterministic proficiency calculations for the demo vertical slice.

The HLD requires level decisions to live in the deterministic domain layer,
not inside an agent. Thresholds here are synthetic demo rules and should be
replaced by the approved competency-level mapping when the real data load is
connected.
"""

FINOPS_LEVELS = ["L0", "L1", "L2", "L3", "L4", "L5"]
RD_LEVELS = ["Beginner", "Moderate", "Expert"]


def ordinal(framework: str, level: str) -> int:
    levels = FINOPS_LEVELS if framework.lower() == "finops" else RD_LEVELS
    return levels.index(level)


def level_from_score(framework: str, score: float) -> str:
    """Map a percentage to a level using deterministic demo thresholds."""
    if framework.lower() == "finops":
        if score < 20: return "L0"
        if score < 40: return "L1"
        if score < 60: return "L2"
        if score < 75: return "L3"
        if score < 90: return "L4"
        return "L5"
    if score < 50: return "Beginner"
    if score < 80: return "Moderate"
    return "Expert"


def calculate_gap(framework: str, current: str, target: str) -> int:
    return max(0, ordinal(framework, target) - ordinal(framework, current))


def gap_label(gap: int) -> str:
    if gap == 0: return "On Target"
    if gap == 1: return "Develop"
    return "Priority Development"
