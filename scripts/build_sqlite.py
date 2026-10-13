"""
build_sqlite.py

Step 2 (this version): Schema creation only.
Creates db/talent360i.sqlite with 6 empty tables, correct types and keys.
No data is loaded yet -- that comes in the next step.

Safe to re-run: drops and recreates every table each time, so the schema
stays in sync with this file no matter what state the .db was left in before.
"""

import sqlite3
from pathlib import Path

import pandas as pd

# Resolve paths relative to this script's own location, not the current
# working directory -- so it works the same regardless of where/how it's run,
# and on any PC/OS, per our portability plan.
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DB_PATH = PROJECT_ROOT / "db" / "talent360i.sqlite"
EXCEL_PATH = PROJECT_ROOT / "data" / "Talent360i_Auto_Assessment_Hackfest_Dataset_v2_Personal.xlsx"

# The real header row in every sheet of this workbook is row index 2
# (rows 0-1 are a title row and a blank row) -- confirmed by inspection.
EXCEL_HEADER_ROW = 2

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE role_master (
        role_id TEXT PRIMARY KEY,
        tower TEXT,
        process_group TEXT,
        role_name TEXT,
        role_grade TEXT,
        level_group TEXT
    );
    """,
    """
    CREATE TABLE role_lineage (
        role_id TEXT PRIMARY KEY,
        mvp_use TEXT,
        synthetic_users_per_role INTEGER,
        source_role_id TEXT,
        source_file TEXT,
        source_sheet TEXT
    );
    """,
    """
    CREATE TABLE role_descriptions (
        role_id TEXT PRIMARY KEY,
        role_summary TEXT,
        responsibility_extract TEXT,
        output_extract TEXT,
        skills_extract TEXT,
        source_type TEXT,
        selection_status TEXT
    );
    """,
    """
    CREATE TABLE skill_master (
        skill_id TEXT PRIMARY KEY,
        tower TEXT,
        skill TEXT,
        capability TEXT,
        l1_indicator TEXT,
        l2_indicator TEXT,
        l3_indicator TEXT,
        l4_indicator TEXT,
        l5_indicator TEXT
    );
    """,
    """
    CREATE TABLE proficiency_levels (
        level INTEGER PRIMARY KEY,
        level_name TEXT,
        behavior_indicator TEXT,
        assessment_design_interpretation TEXT
    );
    """,
    """
    CREATE TABLE role_skill_map (
        role_skill_id TEXT PRIMARY KEY,
        role_id TEXT,
        skill_id TEXT,
        skill TEXT,
        capability TEXT,
        target_proficiency_level INTEGER,
        target_band TEXT,
        behavior_indicator TEXT,
        is_critical INTEGER DEFAULT 0,
        assess_method TEXT DEFAULT 'question',
        is_synthetic_grounding INTEGER DEFAULT 0
    );
    """,
    """
    CREATE TABLE blueprints (
        blueprint_id TEXT PRIMARY KEY,
        role_id TEXT,
        role_name TEXT,
        role_grade TEXT,
        target_question_count INTEGER,
        duration_minutes INTEGER,
        skill_count INTEGER
    );
    """,
    """
    CREATE TABLE training_catalogue (
        course_id TEXT PRIMARY KEY,
        tower TEXT,
        course_title TEXT,
        delivery_type TEXT,
        level_group TEXT,
        risk_level TEXT,
        spoc_role TEXT
    );
    """,
    """
    CREATE TABLE training_skill_map (
        training_skill_map_id TEXT PRIMARY KEY,
        course_id TEXT,
        tower TEXT,
        skill_id TEXT,
        skill TEXT,
        capability TEXT,
        level_group TEXT,
        mapping_confidence TEXT
    );
    """,
    """
    CREATE TABLE skill_gaps (
        gap_id TEXT PRIMARY KEY,
        user_id TEXT,
        role_id TEXT,
        team_id TEXT,
        skill_id TEXT,
        skill TEXT,
        capability TEXT,
        target_level INTEGER,
        current_level REAL,
        gap_level_formula TEXT,
        gap_severity TEXT,
        recommended_course_id TEXT,
        tni_recommendation TEXT,
        cross_functional_recommendation TEXT
    );
    """,
]

TABLE_NAMES = [
    "role_master",
    "role_lineage",
    "role_descriptions",
    "skill_master",
    "proficiency_levels",
    "role_skill_map",
    "blueprints",
    "training_catalogue",
    "training_skill_map",
    "skill_gaps",
]


def build_schema():
    # Make sure the db/ folder exists (it's gitignored, so it won't exist
    # on a fresh clone until this script creates it).
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Drop first so re-running this script always yields a clean, matching schema.
    for table in TABLE_NAMES:
        cur.execute(f"DROP TABLE IF EXISTS {table};")

    for statement in SCHEMA_STATEMENTS:
        cur.execute(statement)

    conn.commit()
    conn.close()

    print(f"Schema created at: {DB_PATH}")
    print(f"Tables created: {', '.join(TABLE_NAMES)}")


def load_role_master():
    """
    Reads the Role_Master sheet from the source Excel file and loads it
    into the role_master table. Only the 6 columns we decided belong here
    are kept -- the rest (mvp_use, synthetic_users_per_role, source_role_id,
    source_file, source_sheet) will go into role_lineage in a later step.

    Safe to re-run: clears existing rows in role_master before inserting,
    so running this twice doesn't create duplicates.
    """
    df = pd.read_excel(EXCEL_PATH, sheet_name="Role_Master", header=EXCEL_HEADER_ROW)

    # Keep and rename only the columns we decided belong in role_master.
    df = df[
        ["role_id", "tower", "process_group", "role_name", "role_grade", "level_group"]
    ]

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("DELETE FROM role_master;")

    cur.executemany(
        """
        INSERT INTO role_master (role_id, tower, process_group, role_name, role_grade, level_group)
        VALUES (?, ?, ?, ?, ?, ?);
        """,
        df.itertuples(index=False, name=None),
    )

    conn.commit()

    # Read back a couple of rows so we can eyeball the result against Excel.
    cur.execute("SELECT COUNT(*) FROM role_master;")
    count = cur.fetchone()[0]

    cur.execute("SELECT * FROM role_master LIMIT 3;")
    sample_rows = cur.fetchall()

    conn.close()

    print(f"\nrole_master loaded: {count} rows inserted.")
    print("Sample rows:")
    for row in sample_rows:
        print(" ", row)


def load_role_lineage():
    """
    Reads the same Role_Master sheet, but this time keeps only the metadata
    columns we deliberately split out of role_master (see plan discussion):
    mvp_use, synthetic_users_per_role, source_role_id, source_file, source_sheet.

    Safe to re-run: clears existing rows in role_lineage before inserting.
    """
    df = pd.read_excel(EXCEL_PATH, sheet_name="Role_Master", header=EXCEL_HEADER_ROW)

    df = df[
        [
            "role_id",
            "mvp_use",
            "synthetic_users_per_role",
            "source_role_id",
            "source_file",
            "source_sheet",
        ]
    ]

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("DELETE FROM role_lineage;")

    cur.executemany(
        """
        INSERT INTO role_lineage
            (role_id, mvp_use, synthetic_users_per_role, source_role_id, source_file, source_sheet)
        VALUES (?, ?, ?, ?, ?, ?);
        """,
        df.itertuples(index=False, name=None),
    )

    conn.commit()

    cur.execute("SELECT COUNT(*) FROM role_lineage;")
    count = cur.fetchone()[0]

    cur.execute("SELECT * FROM role_lineage LIMIT 3;")
    sample_rows = cur.fetchall()

    conn.close()

    print(f"\nrole_lineage loaded: {count} rows inserted.")
    print("Sample rows:")
    for row in sample_rows:
        print(" ", row)


def load_role_descriptions():
    """
    Reads the Role_Descriptions sheet and loads it into role_descriptions.
    Expect 12 rows: 9 core selected roles + 3 REF_ reference-only roles
    (P2P Associate, Credit Controller, Cash Application Associate) --
    this is intentional, not a bug, since reference roles should stay
    matchable later.

    Safe to re-run: clears existing rows in role_descriptions before inserting.
    """
    df = pd.read_excel(EXCEL_PATH, sheet_name="Role_Descriptions", header=EXCEL_HEADER_ROW)

    df = df.rename(
        columns={
            "role_summary_or_positioning": "role_summary",
            "output_or_success_measure_extract": "output_extract",
            "skills_or_capabilities_extract": "skills_extract",
        }
    )

    df = df[
        [
            "role_id",
            "role_summary",
            "responsibility_extract",
            "output_extract",
            "skills_extract",
            "source_type",
            "selection_status",
        ]
    ]

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("DELETE FROM role_descriptions;")

    cur.executemany(
        """
        INSERT INTO role_descriptions
            (role_id, role_summary, responsibility_extract, output_extract, skills_extract, source_type, selection_status)
        VALUES (?, ?, ?, ?, ?, ?, ?);
        """,
        df.itertuples(index=False, name=None),
    )

    conn.commit()

    cur.execute("SELECT COUNT(*) FROM role_descriptions;")
    count = cur.fetchone()[0]

    cur.execute("SELECT role_id, source_type, selection_status FROM role_descriptions;")
    all_rows = cur.fetchall()

    conn.close()

    print(f"\nrole_descriptions loaded: {count} rows inserted.")
    print("All rows (role_id, source_type, selection_status):")
    for row in all_rows:
        print(" ", row)


def load_skill_master():
    """
    Reads the Skill_Master sheet and loads it into skill_master.
    Expect 28 rows: 16 AP + 12 O2C. No R2R rows here -- that's the known
    gap in the source data (R2R skills only exist inside Role_Skill_Map
    with a behavior_indicator, no full L1-L5 ladder). Not a bug in this
    loader; just reflects what's actually in the workbook.

    Safe to re-run: clears existing rows in skill_master before inserting.
    """
    df = pd.read_excel(EXCEL_PATH, sheet_name="Skill_Master", header=EXCEL_HEADER_ROW)

    df = df.rename(
        columns={
            "L1_indicator": "l1_indicator",
            "L2_indicator": "l2_indicator",
            "L3_indicator": "l3_indicator",
            "L4_indicator": "l4_indicator",
            "L5_indicator": "l5_indicator",
        }
    )

    df = df[
        [
            "skill_id",
            "tower",
            "skill",
            "capability",
            "l1_indicator",
            "l2_indicator",
            "l3_indicator",
            "l4_indicator",
            "l5_indicator",
        ]
    ]

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("DELETE FROM skill_master;")

    cur.executemany(
        """
        INSERT INTO skill_master
            (skill_id, tower, skill, capability, l1_indicator, l2_indicator, l3_indicator, l4_indicator, l5_indicator)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        df.itertuples(index=False, name=None),
    )

    conn.commit()

    cur.execute("SELECT COUNT(*) FROM skill_master;")
    count = cur.fetchone()[0]

    cur.execute("SELECT tower, COUNT(*) FROM skill_master GROUP BY tower;")
    tower_counts = cur.fetchall()

    cur.execute("SELECT skill_id, tower, skill FROM skill_master LIMIT 3;")
    sample_rows = cur.fetchall()

    conn.close()

    print(f"\nskill_master loaded: {count} rows inserted.")
    print(f"By tower: {tower_counts}")
    print("Sample rows:")
    for row in sample_rows:
        print(" ", row)


def load_proficiency_levels():
    """
    Reads the Proficiency_Levels sheet and loads it into proficiency_levels.
    Expect 6 rows: levels 0 through 5.

    Safe to re-run: clears existing rows in proficiency_levels before inserting.
    """
    df = pd.read_excel(EXCEL_PATH, sheet_name="Proficiency_Levels", header=EXCEL_HEADER_ROW)

    df = df[
        ["level", "level_name", "behavior_indicator", "assessment_design_interpretation"]
    ]

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("DELETE FROM proficiency_levels;")

    cur.executemany(
        """
        INSERT INTO proficiency_levels
            (level, level_name, behavior_indicator, assessment_design_interpretation)
        VALUES (?, ?, ?, ?);
        """,
        df.itertuples(index=False, name=None),
    )

    conn.commit()

    cur.execute("SELECT COUNT(*) FROM proficiency_levels;")
    count = cur.fetchone()[0]

    cur.execute("SELECT level, level_name FROM proficiency_levels ORDER BY level;")
    all_rows = cur.fetchall()

    conn.close()

    print(f"\nproficiency_levels loaded: {count} rows inserted.")
    print("All rows (level, level_name):")
    for row in all_rows:
        print(" ", row)


def load_role_skill_map():
    """
    Reads the Role_Skill_Map sheet and loads it into role_skill_map.
    Expect 90 rows: 30 AP + 30 O2C + 30 R2R.

    No FK enforcement on skill_id (deliberate, per our schema plan) --
    R2R rows are known to have a skill_id that won't match any row in
    skill_master, since R2R skills only exist here with a behavior_indicator,
    not a full L1-L5 definition. This function makes that gap visible by
    printing an explicit count of unresolved skill_id values per tower,
    rather than silently ignoring it.

    Safe to re-run: clears existing rows in role_skill_map before inserting.
    """
    df = pd.read_excel(EXCEL_PATH, sheet_name="Role_Skill_Map", header=EXCEL_HEADER_ROW)

    df = df[
        [
            "role_skill_id",
            "role_id",
            "skill_id",
            "skill",
            "capability",
            "target_proficiency_level",
            "target_band",
            "behavior_indicator",
        ]
    ]

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("DELETE FROM role_skill_map;")

    cur.executemany(
        """
        INSERT INTO role_skill_map
            (role_skill_id, role_id, skill_id, skill, capability,
             target_proficiency_level, target_band, behavior_indicator)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """,
        df.itertuples(index=False, name=None),
    )

    conn.commit()

    cur.execute("SELECT COUNT(*) FROM role_skill_map;")
    count = cur.fetchone()[0]

    # Explicitly surface the known R2R skill_id gap: for each tower, how many
    # role_skill_map rows have a skill_id that doesn't exist in skill_master.
    cur.execute(
        """
        SELECT rsm.role_id_tower, COUNT(*)
        FROM (
            SELECT
                CASE
                    WHEN role_id LIKE 'R2R%' THEN 'R2R'
                    WHEN role_id LIKE 'AP%' THEN 'AP'
                    WHEN role_id LIKE 'O2C%' THEN 'O2C'
                    ELSE 'OTHER'
                END AS role_id_tower,
                skill_id
            FROM role_skill_map
        ) AS rsm
        LEFT JOIN skill_master sm ON rsm.skill_id = sm.skill_id
        WHERE sm.skill_id IS NULL
        GROUP BY rsm.role_id_tower;
        """
    )
    unresolved_by_tower = cur.fetchall()

    cur.execute("SELECT role_skill_id, role_id, skill_id, skill FROM role_skill_map LIMIT 3;")
    sample_rows = cur.fetchall()

    conn.close()

    print(f"\nrole_skill_map loaded: {count} rows inserted.")
    print("Sample rows:")
    for row in sample_rows:
        print(" ", row)
    print("\nRows whose skill_id does NOT resolve against skill_master, by tower (expected: R2R=30 only):")
    for row in unresolved_by_tower:
        print(" ", row)


def load_blueprints():
    """
    Reads the Assessment_Blueprints sheet and loads it into the blueprints
    table. Expect 9 rows (one per role). This provides the fixed question
    budget (target_question_count) and duration that Agent 1's allocation
    logic needs.

    Safe to re-run: clears existing rows in blueprints before inserting.
    """
    df = pd.read_excel(EXCEL_PATH, sheet_name="Assessment_Blueprints", header=EXCEL_HEADER_ROW)

    df = df[
        [
            "blueprint_id",
            "role_id",
            "role_name",
            "role_grade",
            "target_question_count",
            "duration_minutes",
            "skill_count",
        ]
    ]

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("DELETE FROM blueprints;")

    cur.executemany(
        """
        INSERT INTO blueprints
            (blueprint_id, role_id, role_name, role_grade, target_question_count, duration_minutes, skill_count)
        VALUES (?, ?, ?, ?, ?, ?, ?);
        """,
        df.itertuples(index=False, name=None),
    )

    conn.commit()

    cur.execute("SELECT COUNT(*) FROM blueprints;")
    count = cur.fetchone()[0]

    cur.execute("SELECT blueprint_id, role_id, target_question_count, duration_minutes FROM blueprints LIMIT 3;")
    sample_rows = cur.fetchall()

    conn.close()

    print(f"\nblueprints loaded: {count} rows inserted.")
    print("Sample rows:")
    for row in sample_rows:
        print(" ", row)


def derive_critical_flags():
    """
    Derives is_critical on role_skill_map rows using Assessment_QBank data.

    The QBank has a critical_flag per question. A skill is considered
    critical for a role if ANY of its questions are marked critical_flag='Yes'.
    This function reads the QBank from Excel, builds the set of critical
    (role_id, skill) pairs, then UPDATEs the matching role_skill_map rows.

    Also sets assess_method to 'question' for all FinOps rows explicitly.
    When RD is added later, behavioural competencies will get 'evidence'.

    Runs AFTER load_role_skill_map(), since it updates rows that must
    already exist.

    Safe to re-run: resets all rows to defaults first, then applies flags.
    """
    qb = pd.read_excel(EXCEL_PATH, sheet_name="Assessment_QBank", header=EXCEL_HEADER_ROW)

    # Build set of (role_id, skill) pairs where any question is critical
    critical_pairs = set()
    for _, row in qb[qb["critical_flag"] == "Yes"].iterrows():
        critical_pairs.add((row["role_id"], row["skill"]))

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Reset all rows to defaults first (idempotent)
    cur.execute("UPDATE role_skill_map SET is_critical = 0, assess_method = 'question';")

    # Set is_critical = 1 for each matching (role_id, skill) pair
    for role_id, skill in critical_pairs:
        cur.execute(
            "UPDATE role_skill_map SET is_critical = 1 WHERE role_id = ? AND skill = ?;",
            (role_id, skill),
        )

    conn.commit()

    # Report what was flagged
    cur.execute("SELECT COUNT(*) FROM role_skill_map WHERE is_critical = 1;")
    critical_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM role_skill_map;")
    total_count = cur.fetchone()[0]

    cur.execute(
        """
        SELECT role_id, skill, is_critical FROM role_skill_map
        WHERE is_critical = 1
        ORDER BY role_id
        LIMIT 5;
        """
    )
    sample_rows = cur.fetchall()

    conn.close()

    print(f"\ncritical flags derived: {critical_count} of {total_count} role-skill rows marked critical.")
    print("Sample critical rows:")
    for row in sample_rows:
        print(" ", row)


# Known upstream data gap (see Source_Analysis sheet, SRC-02 "R2R
# role-to-skill mapping XLSX"): every R2R role_skill_map row has
# behavior_indicator and capability blank in the source workbook -- not a
# loader bug, confirmed by reading the raw sheet directly. Without this,
# Agent 2 (agents/agent2_question.py) correctly refuses to generate
# questions for any R2R skill, since there's nothing to ground them on.
#
# The entries below are DERIVED, NOT REAL SME-AUTHORED CONTENT: each one
# follows the same "Independently ... / Consulted for ..." phrasing
# pattern already used by the real AP/O2C entries at the matching
# proficiency level (see Proficiency_Levels.behavior_indicator for the
# generic per-level phrasing this mirrors), applied to the R2R skill name
# and the role's own responsibility_extract context (bank recs, SOX/IPE
# evidence, close, journals -- see Role_Descriptions). They exist so the
# question-generation pipeline can be demoed end-to-end for RD roles;
# every row this touches is flagged is_synthetic_grounding = 1 so nothing
# downstream mistakes this for real SME-validated data. Replace with real
# SME input before this ever reaches an actual candidate.
R2R_SYNTHETIC_GROUNDING = {
    # Level 3 / Intermediate
    "R2R_first_time_right_quality_sox_icfr_ready": (
        "Independently delivers SOX/ICFR-ready, right-first-time reconciliations and "
        "journal entries; prevents control exceptions; improves rework and error rates.",
        "Process Governance",
    ),
    "R2R_r2r_close_journals_us_gaap_ifrs": (
        "Independently prepares and posts standard journal entries and close activities "
        "under US GAAP/IFRS; resolves routine discrepancies; meets close deadlines.",
        "Technical Expertise",
    ),
    "R2R_fixed_assets_banking_ias_16_ias_36_asc_360": (
        "Independently maintains fixed asset and banking schedules under IAS 16/IAS "
        "36/ASC 360; resolves routine variances; supports close readiness.",
        "Technical Expertise",
    ),
    "R2R_ownership_delivery": (
        "Independently owns assigned reconciliations and postings end-to-end; manages "
        "exceptions and handoffs; accountable for on-time, evidence-backed delivery.",
        "Process Governance",
    ),
    # Level 4 / Advanced
    "R2R_attention_to_detail_sox_evidence_trail_documentation": (
        "Consulted for complex SOX evidence and documentation gaps; designs evidence "
        "templates and review checklists; mentors others on audit-ready documentation.",
        "Process Governance",
    ),
    "R2R_reconciliations_clearing_sox_ipe_balance_sheet_recs": (
        "Consulted for complex, aged, or high-risk balance sheet reconciliations; designs "
        "clearing strategies and control checkpoints; mentors others on SOX/IPE-ready "
        "reconciliation practice.",
        "Technical Expertise",
    ),
    "R2R_financial_reporting_ifrs_us_gaap": (
        "Consulted for complex IFRS/US GAAP reporting judgments; designs disclosure and "
        "reporting controls; mentors others on technical reporting positions.",
        "Technical Expertise",
    ),
    "R2R_consolidation_ifrs_10_ifrs_3_ias_21_asc_810": (
        "Consulted for complex consolidation, business combination, and FX translation "
        "matters (IFRS 10/IFRS 3/IAS 21/ASC 810); designs consolidation controls; mentors "
        "others.",
        "Technical Expertise",
    ),
    "R2R_time_management": (
        "Consulted for prioritizing complex, competing close deadlines; designs "
        "workload/queue management approaches; mentors others on time management under "
        "close pressure.",
        "Foundational / Behavioural",
    ),
    "R2R_productivity_tools_excel_ppt": (
        "Consulted for building complex Excel-based reconciliation/reporting tools and "
        "close-related PPT materials; designs reusable templates; trains others on "
        "efficient tool use.",
        "Foundational / Behavioural",
    ),
    "R2R_erp_finance_tools_sap_oracle_blackline_onestream_sox_controls_in_systems": (
        "Consulted for complex ERP/close-tool configurations and SOX controls-in-systems "
        "issues; designs system-level control checkpoints; trains others on the toolset.",
        "Technical Expertise",
    ),
    "R2R_professional_conduct": (
        "Consulted as a model of professional conduct under audit and close pressure; "
        "sets expectations for the team; mentors others on stakeholder-facing "
        "professionalism.",
        "Foundational / Behavioural",
    ),
    "R2R_teamwork_collaboration": (
        "Consulted to resolve cross-team close/reconciliation roadblocks; drives "
        "collaboration across RTR sub-teams and upstream/downstream functions; mentors "
        "others on effective collaboration.",
        "Foundational / Behavioural",
    ),
}


def backfill_r2r_grounding():
    """
    Backfills behavior_indicator/capability for every R2R role_skill_map
    row from R2R_SYNTHETIC_GROUNDING above, and marks each one
    is_synthetic_grounding = 1. See the comment above that dict for why
    this exists and why it is NOT real SME-authored data.

    Runs AFTER load_role_skill_map(). Safe to re-run: only ever touches
    rows matching a known R2R skill_id, and always re-applies the same
    values, so re-running never drifts or duplicates.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    for skill_id, (behavior_indicator, capability) in R2R_SYNTHETIC_GROUNDING.items():
        cur.execute(
            """
            UPDATE role_skill_map
            SET behavior_indicator = ?, capability = ?, is_synthetic_grounding = 1
            WHERE skill_id = ?;
            """,
            (behavior_indicator, capability, skill_id),
        )

    conn.commit()

    cur.execute(
        "SELECT COUNT(*) FROM role_skill_map WHERE is_synthetic_grounding = 1;"
    )
    backfilled_count = cur.fetchone()[0]

    cur.execute(
        """
        SELECT role_id, skill, behavior_indicator FROM role_skill_map
        WHERE is_synthetic_grounding = 1
        ORDER BY role_id
        LIMIT 3;
        """
    )
    sample_rows = cur.fetchall()

    conn.close()

    print(f"\nR2R synthetic grounding backfilled: {backfilled_count} role-skill rows.")
    print("Sample rows:")
    for row in sample_rows:
        print(" ", row)


def load_training_catalogue():
    """
    Reads the Training_Catalogue sheet and loads it into training_catalogue.
    Expect 31 rows (TRN-0001 .. TRN-0031).

    Known upstream data hole (see Source_Analysis sheet): Training_Skill_Map
    and Skill_Gaps_TNI both reference course ids TRN-0032..TRN-0043 -- 12
    ids, all for R2R skills -- that have NO row anywhere in this sheet.
    TRN-0031 itself is present but half-filled: its course_title is the
    bare template "Role-based assessment remediation for " with the skill
    name never filled in. Net effect: there is not one usable R2R course
    in the catalogue. Not a loader bug -- confirmed by reading the raw
    sheet. This is what makes Agent 5 (agents/agent5_devplan.py) report
    every R2R person's confirmed gaps as unaddressable.

    Safe to re-run: clears existing rows in training_catalogue before inserting.
    """
    df = pd.read_excel(EXCEL_PATH, sheet_name="Training_Catalogue", header=EXCEL_HEADER_ROW)

    df = df[
        ["course_id", "tower", "course_title", "delivery_type", "level_group", "risk_level", "spoc_role"]
    ]
    df = df.astype(object).where(pd.notnull(df), None)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("DELETE FROM training_catalogue;")

    cur.executemany(
        """
        INSERT INTO training_catalogue
            (course_id, tower, course_title, delivery_type, level_group, risk_level, spoc_role)
        VALUES (?, ?, ?, ?, ?, ?, ?);
        """,
        df.itertuples(index=False, name=None),
    )

    conn.commit()

    cur.execute("SELECT COUNT(*) FROM training_catalogue;")
    count = cur.fetchone()[0]

    cur.execute("SELECT course_id, tower, course_title FROM training_catalogue LIMIT 3;")
    sample_rows = cur.fetchall()

    conn.close()

    print(f"\ntraining_catalogue loaded: {count} rows inserted.")
    print("Sample rows:")
    for row in sample_rows:
        print(" ", row)


def load_training_skill_map():
    """
    Reads the Training_Skill_Map sheet and loads it into training_skill_map.
    Expect 43 rows.

    No FK enforcement on course_id (deliberate, same reasoning as
    load_role_skill_map()'s R2R skill_id gap): 12 of these rows point at a
    course_id that doesn't exist in training_catalogue (see that loader's
    docstring). This function surfaces that count explicitly rather than
    letting it disappear into a silent join miss downstream.

    Safe to re-run: clears existing rows in training_skill_map before inserting.
    """
    df = pd.read_excel(EXCEL_PATH, sheet_name="Training_Skill_Map", header=EXCEL_HEADER_ROW)

    df = df[
        [
            "training_skill_map_id", "course_id", "tower", "skill_id", "skill",
            "capability", "level_group", "mapping_confidence",
        ]
    ]
    df = df.astype(object).where(pd.notnull(df), None)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("DELETE FROM training_skill_map;")

    cur.executemany(
        """
        INSERT INTO training_skill_map
            (training_skill_map_id, course_id, tower, skill_id, skill, capability, level_group, mapping_confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """,
        df.itertuples(index=False, name=None),
    )

    conn.commit()

    cur.execute("SELECT COUNT(*) FROM training_skill_map;")
    count = cur.fetchone()[0]

    cur.execute(
        """
        SELECT COUNT(DISTINCT tsm.course_id)
        FROM training_skill_map tsm
        LEFT JOIN training_catalogue tc ON tc.course_id = tsm.course_id
        WHERE tc.course_id IS NULL;
        """
    )
    unresolved_course_count = cur.fetchone()[0]

    conn.close()

    print(f"\ntraining_skill_map loaded: {count} rows inserted.")
    print(
        f"Distinct course_id values that do NOT resolve against training_catalogue "
        f"(expected: 12 -- TRN-0032..TRN-0043): {unresolved_course_count}"
    )


def load_skill_gaps():
    """
    Reads the Skill_Gaps_TNI sheet and loads it into skill_gaps. Expect 360
    rows (36 synthetic users x 10 gap rows each).

    gap_level_formula is TEXT, not INTEGER, on purpose: the source column
    mixes numeric gap widths ("0", "1", "2", "3") with the literal string
    "Assessment Pending" for the 100 rows where current_level was never
    assessed (confirmed by reading the raw sheet -- pandas infers the
    whole column as object/str). gap_severity is NULL for those same 100
    rows; Agent 5 (agents/agent5_devplan.py) treats NULL gap_severity as
    "never assessed", distinct from "No gap" (at target) and
    "Moderate"/"High" (confirmed gap).

    Safe to re-run: clears existing rows in skill_gaps before inserting.
    """
    df = pd.read_excel(EXCEL_PATH, sheet_name="Skill_Gaps_TNI", header=EXCEL_HEADER_ROW)

    df = df[
        [
            "gap_id", "user_id", "role_id", "team_id", "skill_id", "skill", "capability",
            "target_level", "current_level", "gap_level_formula", "gap_severity",
            "recommended_course_id", "tni_recommendation", "cross_functional_recommendation",
        ]
    ]
    df = df.astype(object).where(pd.notnull(df), None)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("DELETE FROM skill_gaps;")

    cur.executemany(
        """
        INSERT INTO skill_gaps
            (gap_id, user_id, role_id, team_id, skill_id, skill, capability,
             target_level, current_level, gap_level_formula, gap_severity,
             recommended_course_id, tni_recommendation, cross_functional_recommendation)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        df.itertuples(index=False, name=None),
    )

    conn.commit()

    cur.execute("SELECT COUNT(*) FROM skill_gaps;")
    count = cur.fetchone()[0]

    cur.execute("SELECT gap_severity, COUNT(*) FROM skill_gaps GROUP BY gap_severity;")
    severity_counts = cur.fetchall()

    cur.execute(
        """
        SELECT COUNT(DISTINCT sg.recommended_course_id)
        FROM skill_gaps sg
        LEFT JOIN training_catalogue tc ON tc.course_id = sg.recommended_course_id
        WHERE tc.course_id IS NULL AND sg.recommended_course_id IS NOT NULL;
        """
    )
    unresolved_recommendation_count = cur.fetchone()[0]

    conn.close()

    print(f"\nskill_gaps loaded: {count} rows inserted.")
    print(f"By gap_severity (None = never assessed): {severity_counts}")
    print(
        f"Distinct recommended_course_id values that do NOT resolve against "
        f"training_catalogue: {unresolved_recommendation_count}"
    )


if __name__ == "__main__":
    import sys
    # Windows terminals default stdout to cp1252, which chokes on the
    # unicode arrows in this script's sample-row prints -- force UTF-8,
    # same fix used in agents/agent2_question.py's __main__ block.
    sys.stdout.reconfigure(encoding="utf-8")
    build_schema()
    load_role_master()
    load_role_lineage()
    load_role_descriptions()
    load_skill_master()
    load_proficiency_levels()
    load_role_skill_map()
    load_blueprints()
    derive_critical_flags()
    backfill_r2r_grounding()
    load_training_catalogue()
    load_training_skill_map()
    load_skill_gaps()