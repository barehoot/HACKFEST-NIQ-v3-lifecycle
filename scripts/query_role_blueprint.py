"""
query_role_blueprint.py

Sub-step 6 (Phase 2 wrap-up): a read-only lookup tool that proves Step B
(role -> skills -> target levels -> behavior -> proficiency interpretation)
works as a single reliable join, no AI involved.

This is meant to be reused later while developing Agent 2 -- "show me the
blueprint for role X" -- not a one-off script.

Usage:
    python scripts/query_role_blueprint.py <role_id>
    python scripts/query_role_blueprint.py           (defaults to AP_p2p_lead)
"""

import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DB_PATH = PROJECT_ROOT / "db" / "talent360i.sqlite"

DEFAULT_ROLE_ID = "AP_p2p_lead"


def get_role_blueprint(role_id: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Confirm the role exists at all, before joining further -- gives a
    # clear error instead of silently returning zero rows for a typo'd role_id.
    cur.execute("SELECT role_id, role_name, tower, role_grade FROM role_master WHERE role_id = ?;", (role_id,))
    role_row = cur.fetchone()

    if role_row is None:
        conn.close()
        print(f"No role found with role_id = '{role_id}'.")
        print("Tip: check exact spelling/casing against the role_master table.")
        return

    print(f"=== Blueprint lookup for: {role_row[1]} ({role_row[0]}) ===")
    print(f"Tower: {role_row[2]} | Grade: {role_row[3]}\n")

    # The core join: this role's skills, their target level, the
    # behavior_indicator from role_skill_map, the proficiency_levels
    # interpretation for that level, and the skill_master L1-L5 ladder
    # where it exists (LEFT JOIN, since R2R skills won't resolve here --
    # that's the known, expected gap).
    cur.execute(
        """
        SELECT
            rsm.skill,
            rsm.capability,
            rsm.target_proficiency_level,
            pl.level_name,
            pl.assessment_design_interpretation,
            rsm.behavior_indicator,
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
        ORDER BY rsm.target_proficiency_level DESC;
        """,
        (role_id,),
    )
    rows = cur.fetchall()
    conn.close()

    print(f"Skills mapped to this role: {len(rows)}\n")

    for row in rows:
        (
            skill, capability, target_level, level_name, level_interpretation,
            behavior_indicator, resolved_skill_id, l1, l2, l3, l4, l5,
        ) = row

        print(f"- Skill: {skill}")
        print(f"  Capability: {capability}")
        print(f"  Target level: {target_level} ({level_name})")
        print(f"  How to design a question at this level: {level_interpretation}")
        print(f"  Role-specific behavior indicator: {behavior_indicator}")

        if resolved_skill_id is None:
            print("  L1-L5 skill ladder: NOT AVAILABLE (known gap -- this skill has no "
                  "skill_master entry, expected for R2R skills).")
        else:
            level_text = {1: l1, 2: l2, 3: l3, 4: l4, 5: l5}.get(target_level)
            print(f"  L{target_level} indicator text: {level_text}")

        print()


if __name__ == "__main__":
    role_id_arg = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ROLE_ID
    get_role_blueprint(role_id_arg)