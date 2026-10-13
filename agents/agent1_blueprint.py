"""
agent1_blueprint.py

Sub-step 14 (Phase 5): Agent 1's blueprint-building step.

Takes a role_id, returns a structured blueprint dict -- the same join
logic proven in scripts/query_role_blueprint.py (sub-step 6), but
returning data instead of printing it, and implementing our Option 2
decision: skills with missing capability/behavior_indicator (the known
R2R gap) are tagged detail_status = "insufficient - needs SME input"
rather than passed downstream with silent blanks.

The R2R gap has since been backfilled with clearly-synthetic grounding
data (see scripts/build_sqlite.py's R2R_SYNTHETIC_GROUNDING /
backfill_r2r_grounding, and role_skill_map.is_synthetic_grounding) so
Agent 2 can generate RD questions too -- but a skill grounded that way is
still tagged distinctly here (detail_status = "complete - synthetic
grounding (not SME-validated)"), not silently folded into "complete", so
nothing downstream mistakes it for real SME-authored content.
"""

import sqlite3
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = AGENTS_DIR.parent
DB_PATH = PROJECT_ROOT / "db" / "talent360i.sqlite"


def build_blueprint(role_id: str):
    """
    Returns a structured blueprint dict for the given role_id, or a dict
    with success=False if the role doesn't exist.

    Shape:
        {
            "success": True,
            "role_id": ...,
            "role_name": ...,
            "tower": ...,
            "role_grade": ...,
            "skill_count": ...,
            "skills": [
                {
                    "skill": ...,
                    "capability": ... or None,
                    "target_proficiency_level": ...,
                    "target_level_name": ...,
                    "assessment_design_interpretation": ...,
                    "behavior_indicator": ... or None,
                    "l_indicator_text": ... or None,
                    "is_synthetic_grounding": bool,
                    "detail_status": "complete" | "complete - synthetic grounding (not SME-validated)" | "insufficient - needs SME input",
                },
                ...
            ],
        }
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute(
        "SELECT role_id, role_name, tower, role_grade FROM role_master WHERE role_id = ?;",
        (role_id,),
    )
    role_row = cur.fetchone()

    if role_row is None:
        conn.close()
        return {
            "success": False,
            "message": f"No role found with role_id = '{role_id}'.",
        }

    # Fetch question budget from the blueprints table.
    # May not exist for REF_ roles (they have no blueprint row), so
    # default to None rather than crashing.
    cur.execute(
        "SELECT target_question_count, duration_minutes FROM blueprints WHERE role_id = ?;",
        (role_id,),
    )
    bp_row = cur.fetchone()
    target_question_count = bp_row[0] if bp_row else None
    duration_minutes = bp_row[1] if bp_row else None

    cur.execute(
        """
        SELECT
            rsm.skill,
            rsm.capability,
            rsm.target_proficiency_level,
            pl.level_name,
            pl.assessment_design_interpretation,
            rsm.behavior_indicator,
            rsm.is_critical,
            rsm.assess_method,
            rsm.is_synthetic_grounding,
            sm.skill_id,
            sm.l1_indicator,
            sm.l2_indicator,
            sm.l3_indicator,
            sm.l4_indicator,
            sm.l5_indicator
        FROM role_skill_map rsm
        LEFT JOIN proficiency_levels pl ON rsm.target_proficiency_level = pl.level
        LEFT JOIN skill_master sm ON rsm.skill_id = sm.skill_id
        WHERE rsm.role_id = ?
        ORDER BY rsm.is_critical DESC, rsm.target_proficiency_level DESC;
        """,
        (role_id,),
    )
    rows = cur.fetchall()
    conn.close()

    skills = []
    for row in rows:
        (
            skill, capability, target_level, level_name, level_interpretation,
            behavior_indicator, is_critical, assess_method, is_synthetic_grounding,
            resolved_skill_id, l1, l2, l3, l4, l5,
        ) = row

        if resolved_skill_id is None:
            l_indicator_text = None
        else:
            l_indicator_text = {1: l1, 2: l2, 3: l3, 4: l4, 5: l5}.get(target_level)

        # Option 2 decision: flag missing detail explicitly rather than
        # passing None/blank fields downstream silently. A skill is
        # "insufficient" if it's missing either the behavior_indicator
        # or the capability grouping. R2R rows have since been backfilled
        # with synthetic grounding (see module docstring) -- those are
        # marked "complete", but distinctly, since it's not real
        # SME-authored content.
        if behavior_indicator is None or capability is None:
            detail_status = "insufficient - needs SME input"
        elif is_synthetic_grounding:
            detail_status = "complete - synthetic grounding (not SME-validated)"
        else:
            detail_status = "complete"

        skills.append(
            {
                "skill": skill,
                "capability": capability,
                "target_proficiency_level": target_level,
                "target_level_name": level_name,
                "assessment_design_interpretation": level_interpretation,
                "behavior_indicator": behavior_indicator,
                "l_indicator_text": l_indicator_text,
                "level_indicators": {1: l1, 2: l2, 3: l3, 4: l4, 5: l5} if resolved_skill_id else {},
                "is_critical": bool(is_critical),
                "assess_method": assess_method,
                "is_synthetic_grounding": bool(is_synthetic_grounding),
                "detail_status": detail_status,
            }
        )

    return {
        "success": True,
        "role_id": role_row[0],
        "role_name": role_row[1],
        "tower": role_row[2],
        "role_grade": role_row[3],
        "target_question_count": target_question_count,
        "duration_minutes": duration_minutes,
        "skill_count": len(skills),
        "skills": skills,
    }


def with_assessment_level(blueprint, level):
    """
    Copy of a blueprint re-targeted to one proficiency level chosen by the
    SME (1-5) instead of each skill's role target. Each skill is grounded on
    skill_master's own L-indicator for that level, so a question written
    "at L2" describes L2 behaviour; a skill without that text keeps the
    role's behavior_indicator. The original blueprint is not modified.
    """
    if not blueprint.get("success"):
        return blueprint
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT level_name, assessment_design_interpretation FROM proficiency_levels WHERE level = ?;",
            (level,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError(f"Unknown proficiency level {level}.")
    level_name, interpretation = row
    skills = [
        {
            **skill,
            "target_proficiency_level": level,
            "target_level_name": level_name,
            "assessment_design_interpretation": interpretation,
            "behavior_indicator": (skill.get("level_indicators") or {}).get(level) or skill["behavior_indicator"],
        }
        for skill in blueprint["skills"]
    ]
    return {**blueprint, "skills": skills, "assessment_level": level}


def print_blueprint_summary(role_id):
    """
    Test helper: builds a blueprint and prints a compact summary --
    one line per skill showing critical flag, assess_method, and
    detail_status -- plus the question budget from the blueprints table.
    """
    result = build_blueprint(role_id)

    if not result["success"]:
        print(result["message"])
        return

    print(f"=== {result['role_name']} ({result['role_id']}) ===")
    print(f"Tower: {result['tower']} | Grade: {result['role_grade']} | Skills: {result['skill_count']}")
    print(f"Question budget: {result['target_question_count']} items | Duration: {result['duration_minutes']} min\n")

    complete_count = sum(1 for s in result["skills"] if s["detail_status"] == "complete")
    synthetic_count = sum(1 for s in result["skills"] if s["is_synthetic_grounding"])
    insufficient_count = result["skill_count"] - complete_count - synthetic_count
    critical_count = sum(1 for s in result["skills"] if s["is_critical"])

    for s in result["skills"]:
        if s["detail_status"] == "complete":
            detail_marker = "OK"
        elif s["is_synthetic_grounding"]:
            detail_marker = "SYN"
        else:
            detail_marker = "!!"
        crit_marker = "CRIT" if s["is_critical"] else "    "
        print(f"  [{detail_marker}] [{crit_marker}] {s['skill']} (L{s['target_proficiency_level']}, {s['assess_method']})")

    print(
        f"\nSummary: {complete_count} complete, {synthetic_count} complete "
        f"(synthetic grounding), {insufficient_count} insufficient, {critical_count} critical\n"
    )


if __name__ == "__main__":
    # One role with full detail (AP), one with the known R2R gap --
    # same two cases we validated by hand in sub-step 6.
    print_blueprint_summary("AP_p2p_lead")
    print_blueprint_summary("R2R_banking_preparer_job_preparation_cash_posting")
    print_blueprint_summary("NOT_A_REAL_ROLE")