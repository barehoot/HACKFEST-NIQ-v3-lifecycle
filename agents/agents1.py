"""
agent1.py

Sub-step 15 (Phase 5): Agent 1, wired end to end.

Chains agent1_matcher.match_resume() -> agent1_blueprint.build_blueprint(),
handling all three outcomes from the matcher:
    - auto_matched: build and return the blueprint for the top match.
    - needs_human_confirmation: build blueprints for BOTH candidates,
      so a human reviewing has full context either way, and flag the
      result for review rather than silently picking one.
    - no_match_found: return a clean failure.

This is the actual Agent 1 entry point: run_agent1(resume_text) -> result.
"""

import sys
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENTS_DIR))

from agent1_matcher import match_resume  # noqa: E402
from agent1_blueprint import build_blueprint  # noqa: E402
from resume_parser import extract_resume_text  # noqa: E402


def run_agent1(resume_text):
    """
    Full Agent 1 pipeline: resume text -> role match -> blueprint(s).

    Returns a dict:
        {
            "status": "auto_matched" | "needs_human_confirmation"
                      | "no_match_found" | "blueprint_unavailable",
            "match_info": {...} or None,   # the raw match_resume() result, for audit/logging
            "blueprint": {...} or None,    # populated only when status == "auto_matched"
            "candidate_blueprints": [...] or None,  # populated only when needs_human_confirmation
        }

    "blueprint_unavailable" means the role matched (auto_matched or
    needs_human_confirmation) but build_blueprint() came back with
    success=False for every candidate -- e.g. the match landed on a
    REF_ reference-only role, which exists in the Chroma collection but
    has no row in role_master. match_info still carries the matched
    role_id(s) so a human can see what was matched even though no
    blueprint could be built.
    """
    match_result = match_resume(resume_text)

    if match_result["status"] == "no_match_found":
        return {
            "status": "no_match_found",
            "match_info": match_result,
            "blueprint": None,
            "candidate_blueprints": None,
        }

    if match_result["status"] == "auto_matched":
        top_role_id = match_result["top_match"]["role_id"]
        blueprint = build_blueprint(top_role_id)

        if not blueprint["success"]:
            return {
                "status": "blueprint_unavailable",
                "match_info": match_result,
                "blueprint": blueprint,
                "candidate_blueprints": None,
            }

        return {
            "status": "auto_matched",
            "match_info": match_result,
            "blueprint": blueprint,
            "candidate_blueprints": None,
        }

    # needs_human_confirmation: build blueprints for both candidates so
    # a human reviewing has full context on either choice, not just
    # role names and a distance score.
    top_role_id = match_result["top_match"]["role_id"]
    second_role_id = match_result["second_match"]["role_id"]

    candidate_blueprints = [
        build_blueprint(top_role_id),
        build_blueprint(second_role_id),
    ]

    if not any(bp["success"] for bp in candidate_blueprints):
        return {
            "status": "blueprint_unavailable",
            "match_info": match_result,
            "blueprint": None,
            "candidate_blueprints": candidate_blueprints,
        }

    return {
        "status": "needs_human_confirmation",
        "match_info": match_result,
        "blueprint": None,
        "candidate_blueprints": candidate_blueprints,
    }


def run_agent1_from_file(file_path):
    """
    Convenience wrapper: uploaded file path -> extract_resume_text() ->
    run_agent1(). Kept separate from run_agent1(resume_text) so that
    signature stays undisturbed for existing/future callers that already
    have plain text; this is purely an additive entry point for callers
    that start from an actual uploaded file (PDF/DOCX/TXT).
    """
    resume_text = extract_resume_text(file_path)
    return run_agent1(resume_text)


def print_agent1_result(resume_label, resume_text):
    """
    Test helper: runs run_agent1() and prints a readable summary of
    which branch fired and what came out of it.
    """
    result = run_agent1(resume_text)

    print(f"--- {resume_label} ---")
    print(f"Overall status: {result['status']}")

    if result["status"] == "auto_matched":
        bp = result["blueprint"]
        print(f"Matched role: {bp['role_name']} ({bp['role_id']})")
        print(f"Skills in blueprint: {bp['skill_count']}")
        insufficient = sum(1 for s in bp["skills"] if s["detail_status"] != "complete")
        print(f"Skills flagged insufficient: {insufficient}")

    elif result["status"] == "needs_human_confirmation":
        print("Two candidates need human review:")
        for bp in result["candidate_blueprints"]:
            if bp["success"]:
                print(f"  - {bp['role_name']} ({bp['role_id']}), {bp['skill_count']} skills")
            else:
                print(f"  - {bp['message']}")
        print(f"Gap between candidates: {result['match_info']['gap']:.4f}")

    elif result["status"] == "blueprint_unavailable":
        top = result["match_info"]["top_match"]
        print(
            f"Matched role_id '{top['role_id']}' but no blueprint could be built "
            "(likely a REF_ reference-only role with no role_master entry)."
        )

    else:
        print("No match found -- resume text may be too far from all known roles.")

    print()


if __name__ == "__main__":
    print_agent1_result(
        "AP-leaning",
        "I process vendor invoices, handle 3-way matching, and resolve payment exceptions in SAP.",
    )
    print_agent1_result(
        "O2C-leaning",
        "I manage customer billing, issue credit notes, and review revenue recognition for accuracy against contracts.",
    )
    print_agent1_result(
        "R2R-leaning (close call, expect needs_human_confirmation)",
        "I prepare bank reconciliations, post journal entries, and ensure balance sheet accounts are supported with SOX evidence.",
    )
    print_agent1_result(
        "Deliberately vague",
        "I work in finance operations and use Excel and SAP for reconciliations and reporting.",
    )