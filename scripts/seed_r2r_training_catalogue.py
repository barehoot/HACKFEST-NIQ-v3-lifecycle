"""
seed_r2r_training_catalogue.py

Fills a genuine gap in the source dataset: data/Talent360i_Auto_Assessment_
Hackfest_Dataset_v2_Personal.xlsx's Training_Catalogue sheet has 17 courses
for AP and 13 for O2C, and literally 0 for R2R (confirmed by inspecting the
sheet directly, not a scripts/build_sqlite.py loading bug). Without any
catalogue row, agents/agent5_devplan.py can find real skill gaps for an R2R
employee but can never cite a verified internal course, so `steps` always
comes back empty for R2R -- Agent 5 was working correctly, the underlying
data just had no R2R entries to draw from (see also: R2R's role_skill_map
rows are documented elsewhere as missing behavior_indicator/capability,
scripts/build_sqlite.py's load_skill_master docstring).

This adds one placeholder course per distinct R2R skill (13), following the
*exact* pattern the dataset's own authors already used for AP gaps with no
mapped training topic (course_id TRN-0004/0005, title "Role-based assessment
remediation for <skill>", level_group "Generated from gap-to-target",
mapping_confidence "Medium - generated placeholder") -- not a new format
invented here, the dataset's own convention applied to the tower it was
never applied to.

Clearly labelled as generated, not real courses (same as the AP precedent).

IMPORTANT: training_catalogue and training_skill_map are two of the 10
reference tables scripts/build_sqlite.py drops and recreates from the xlsx
source on every run. These generated rows live only in this script, not in
the xlsx, so a re-run of build_sqlite.py wipes them -- re-run this script
afterward to restore them. Safe to re-run itself: skips seeding if any R2R
row is already present.
"""

import sqlite3
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DB_PATH = PROJECT_ROOT / "db" / "talent360i.sqlite"

# (skill_id, skill, capability) -- pulled from role_skill_map's own R2R rows.
R2R_SKILLS = [
    ("R2R_first_time_right_quality_sox_icfr_ready", "First Time Right (Quality, SOX/ICFR-ready)", "Process Governance"),
    ("R2R_attention_to_detail_sox_evidence_trail_documentation", "Attention to Detail (SOX evidence trail / documentation)", "Process Governance"),
    ("R2R_r2r_close_journals_us_gaap_ifrs", "R2R Close & Journals (US GAAP / IFRS)", "Technical Expertise"),
    ("R2R_reconciliations_clearing_sox_ipe_balance_sheet_recs", "Reconciliations & Clearing (SOX/IPE balance sheet recs)", "Technical Expertise"),
    ("R2R_fixed_assets_banking_ias_16_ias_36_asc_360", "Fixed Assets & Banking (IAS 16 / IAS 36 / ASC 360)", "Technical Expertise"),
    ("R2R_time_management", "Time Management", "Foundational / Behavioural"),
    ("R2R_productivity_tools_excel_ppt", "Productivity Tools (Excel/PPT)", "Foundational / Behavioural"),
    ("R2R_erp_finance_tools_sap_oracle_blackline_onestream_sox_controls_in_systems", "ERP / Finance Tools (SAP/Oracle/BlackLine/OneStream) (SOX controls-in-systems)", "Technical Expertise"),
    ("R2R_professional_conduct", "Professional Conduct", "Foundational / Behavioural"),
    ("R2R_teamwork_collaboration", "Teamwork / Collaboration", "Foundational / Behavioural"),
    ("R2R_financial_reporting_ifrs_us_gaap", "Financial Reporting (IFRS / US GAAP)", "Technical Expertise"),
    ("R2R_consolidation_ifrs_10_ifrs_3_ias_21_asc_810", "Consolidation (IFRS 10 / IFRS 3 / IAS 21 / ASC 810)", "Technical Expertise"),
    ("R2R_ownership_delivery", "Ownership & Delivery", "Process Governance"),
]


def seed(conn: sqlite3.Connection) -> int:
    already = conn.execute("SELECT COUNT(*) FROM training_catalogue WHERE tower = 'R2R'").fetchone()[0]
    if already:
        return 0

    next_course = conn.execute("SELECT course_id FROM training_catalogue ORDER BY course_id DESC LIMIT 1").fetchone()
    next_course_n = int(next_course[0].split("-")[1]) + 1 if next_course else 1
    next_tsm = conn.execute("SELECT training_skill_map_id FROM training_skill_map ORDER BY training_skill_map_id DESC LIMIT 1").fetchone()
    next_tsm_n = int(next_tsm[0].split("-")[1]) + 1 if next_tsm else 1

    for skill_id, skill, capability in R2R_SKILLS:
        course_id = f"TRN-{next_course_n:04d}"
        conn.execute(
            """
            INSERT INTO training_catalogue (course_id, tower, course_title, delivery_type, level_group, risk_level, spoc_role)
            VALUES (?, 'R2R', ?, 'Scenario Lab / Microlearning / Coaching', 'Generated from gap-to-target', 'Medium', 'Capability Team + SME')
            """,
            (course_id, f"Role-based assessment remediation for {skill}"),
        )
        conn.execute(
            """
            INSERT INTO training_skill_map (training_skill_map_id, course_id, tower, skill_id, skill, capability, level_group, mapping_confidence)
            VALUES (?, ?, 'R2R', ?, ?, ?, 'Generated from gap-to-target', 'Medium - generated placeholder')
            """,
            (f"TSM-{next_tsm_n:04d}", course_id, skill_id, skill, capability),
        )
        next_course_n += 1
        next_tsm_n += 1
    conn.commit()
    return len(R2R_SKILLS)


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        inserted = seed(conn)
    finally:
        conn.close()
    if inserted:
        print(f"Seeded {inserted} generated R2R course(s) + skill mappings into {DB_PATH}.")
    else:
        print("R2R training catalogue already seeded -- nothing to do.")


if __name__ == "__main__":
    main()
