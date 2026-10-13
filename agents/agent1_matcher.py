"""
agent1_matcher.py

Sub-step 13 (Phase 5): Agent 1's role-matching step.

Wraps match_resume_to_role() from scripts/build_chroma.py (already built
and tested) with the confidence-check logic we designed earlier: if the
gap between the top two matches is small, the model itself is unsure
between two similar roles, and that should be flagged for a human to
confirm rather than silently auto-picked.

This is deliberately simple for now (a fixed gap threshold) -- fine-tuning
this logic comes later, once all agents exist end to end.
"""

import sys
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = AGENTS_DIR.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))

from build_chroma import match_resume_to_role  # noqa: E402

# If the distance gap between the top match and the second-best match is
# smaller than this, the two candidates are considered close enough that
# a human should confirm which role is correct, rather than auto-picking
# the top one. Starting value only -- revisit once we have more real
# resume test data.
CONFIDENCE_GAP_THRESHOLD = 0.10


def match_resume(resume_text):
    """
    Runs the resume-to-role match and adds a confidence decision on top.

    Returns a dict:
        {
            "status": "auto_matched" | "needs_human_confirmation",
            "top_match": {role_id, distance, metadata},
            "second_match": {role_id, distance, metadata} or None,
            "gap": float or None,
        }

    status == "needs_human_confirmation" means: don't silently proceed
    to blueprint-building with the top match alone -- surface both
    candidates to a human first. This mirrors the SME_Review_Workflow
    pattern already used elsewhere in this dataset, applied one stage
    earlier (at matching, not just at question approval).
    """
    matches = match_resume_to_role(resume_text, n_results=2)

    if not matches:
        return {
            "status": "no_match_found",
            "top_match": None,
            "second_match": None,
            "gap": None,
        }

    top_match = matches[0]
    second_match = matches[1] if len(matches) > 1 else None

    if second_match is None:
        # Only one role exists to match against -- shouldn't happen with
        # our 12-role collection, but handled explicitly rather than
        # assuming a second match always exists.
        gap = None
        status = "auto_matched"
    else:
        gap = second_match["distance"] - top_match["distance"]
        status = "auto_matched" if gap >= CONFIDENCE_GAP_THRESHOLD else "needs_human_confirmation"

    return {
        "status": status,
        "top_match": top_match,
        "second_match": second_match,
        "gap": gap,
    }


def print_match_result(resume_label, resume_text):
    """
    Test helper: runs match_resume() and prints a readable summary,
    so we can eyeball both the auto-matched and needs-confirmation cases.
    """
    result = match_resume(resume_text)

    print(f"--- {resume_label} ---")
    print(f"Status: {result['status']}")

    top = result["top_match"]
    print(f"Top match: {top['role_id']} (tower: {top['metadata']['tower']}, distance: {top['distance']:.4f})")

    if result["second_match"]:
        second = result["second_match"]
        print(f"Second match: {second['role_id']} (tower: {second['metadata']['tower']}, distance: {second['distance']:.4f})")
        print(f"Gap: {result['gap']:.4f} (threshold: {CONFIDENCE_GAP_THRESHOLD})")

    print()


if __name__ == "__main__":
    # Reuse the three cross-tower samples from sub-step 10, plus one
    # deliberately ambiguous sample that should trigger the
    # needs_human_confirmation path.
    print_match_result(
        "AP-leaning (should auto-match)",
        "I process vendor invoices, handle 3-way matching, and resolve payment exceptions in SAP.",
    )
    print_match_result(
        "O2C-leaning (should auto-match)",
        "I manage customer billing, issue credit notes, and review revenue recognition for accuracy against contracts.",
    )
    print_match_result(
        "R2R-leaning (close call between two R2R roles, from sub-step 10)",
        "I prepare bank reconciliations, post journal entries, and ensure balance sheet accounts are supported with SOX evidence.",
    )
    print_match_result(
        "Deliberately vague / cross-tower blend (likely low confidence)",
        "I work in finance operations and use Excel and SAP for reconciliations and reporting.",
    )