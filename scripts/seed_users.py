"""
seed_users.py

Single entry point for provisioning every portal login account: the 6
built-in role accounts (manager/employee/sme/scheduler/leader/admin) and the
synthetic dataset accounts (SYN-U001..SYN-U036, from the `Users_Teams` sheet
of data/Talent360i_Auto_Assessment_Hackfest_Dataset_v2_Personal.xlsx).

Replaces build_app_tables.py's demo-user seeding, seed_synthetic_users.py,
and reset_demo_passwords.py:
  - No randomly-generated, printed-once, unrecoverable password. Both
    passwords below are REQUIRED env vars -- the script refuses to run
    without them (secure-coding guideline A.2/A.7: no hardcoded secrets,
    but also no silent random default that can get lost).
  - Safe to re-run at any time, including to rotate passwords: user rows
    are upserted, so an existing user's password_hash is always resynced
    to the current env var value, while their role/department/etc. (which
    an admin may have since edited in the UI) are left untouched.
  - Prints every final username next to which password it uses, so
    credentials are always visible in the terminal -- nothing to lose.

Uses bcrypt hashing and parameterised SQL only (see SECURITY.md / A.2, A.4).
This only touches password provisioning -- CSRF protection, login lockout,
and session timeouts in services/security.py and services/auth.py are
unrelated and unchanged.
"""

import os
import sqlite3
import sys
from pathlib import Path

import bcrypt
import openpyxl
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DB_PATH = PROJECT_ROOT / "db" / "talent360i.sqlite"
DATASET_PATH = PROJECT_ROOT / "data" / "Talent360i_Auto_Assessment_Hackfest_Dataset_v2_Personal.xlsx"

load_dotenv(PROJECT_ROOT / ".env")

DEMO_PASSWORD = os.getenv("TALENT360_DEMO_PASSWORD", "")
SYNTHETIC_PASSWORD = os.getenv("TALENT360_SYNTHETIC_PASSWORD", "")
if not DEMO_PASSWORD or not SYNTHETIC_PASSWORD:
    raise SystemExit(
        "TALENT360_DEMO_PASSWORD and TALENT360_SYNTHETIC_PASSWORD must both be "
        "set in .env before seeding users (no hardcoded/random default on purpose)."
    )

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        full_name TEXT,
        role TEXT NOT NULL CHECK(role IN ('employee','sme','scheduler','manager','leader','admin','hr')),
        department TEXT,
        job_title TEXT,
        manager_id TEXT REFERENCES users(user_id),
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS assessment_cycles (
        cycle_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(user_id),
        resume_filename TEXT,
        resume_text TEXT,
        matched_role_id TEXT,
        matched_role_name TEXT,
        matched_role_grade TEXT,
        match_status TEXT,
        status TEXT NOT NULL,
        mcq_score_json TEXT,
        case_study_json TEXT,
        case_study_answers_json TEXT,
        evidence_report_json TEXT,
        dev_plan_json TEXT,
        manager_decision TEXT,
        manager_decision_notes TEXT,
        manager_id TEXT REFERENCES users(user_id),
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS generated_questions (
        question_id TEXT PRIMARY KEY,
        cycle_id TEXT NOT NULL REFERENCES assessment_cycles(cycle_id),
        skill TEXT,
        question_text TEXT,
        option_a TEXT,
        option_b TEXT,
        option_c TEXT,
        option_d TEXT,
        correct_option TEXT,
        difficulty TEXT,
        is_critical INTEGER,
        ai_confidence REAL,
        sme_status TEXT NOT NULL DEFAULT 'pending',
        reviewed_by TEXT REFERENCES users(user_id),
        reviewed_at TEXT,
        item_json TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS assessment_answers (
        cycle_id TEXT NOT NULL REFERENCES assessment_cycles(cycle_id),
        question_id TEXT NOT NULL REFERENCES generated_questions(question_id),
        selected_option TEXT,
        PRIMARY KEY (cycle_id, question_id)
    );
    """,
    # Scheduler calendar. Kept in sync with services/db.py SCHEDULED_EVENTS_SCHEMA
    # in the portal, which also creates it on first use for older databases.
    """
    CREATE TABLE IF NOT EXISTS scheduled_events (
        event_id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL CHECK(event_type IN ('assessment','training')),
        title TEXT NOT NULL,
        user_id TEXT REFERENCES users(user_id),
        role_id TEXT,
        skill TEXT,
        course_id TEXT,
        event_date TEXT NOT NULL,
        start_time TEXT,
        duration_minutes INTEGER,
        status TEXT NOT NULL DEFAULT 'scheduled' CHECK(status IN ('scheduled','completed','cancelled')),
        notes TEXT,
        created_by TEXT REFERENCES users(user_id),
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # Leader calibration reviews. Kept in sync with services/db.py CALIBRATION_SCHEMA.
    """
    CREATE TABLE IF NOT EXISTS calibration_reviews (
        review_id TEXT PRIMARY KEY,
        cycle_id TEXT NOT NULL REFERENCES assessment_cycles(cycle_id),
        leader_id TEXT NOT NULL REFERENCES users(user_id),
        manager_id TEXT REFERENCES users(user_id),
        manager_decision TEXT,
        manager_note TEXT,
        outcome TEXT NOT NULL CHECK(outcome IN ('concur','rereview')),
        note TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # Reusable question bank. Kept in sync with services/db.py BANK_SCHEMA.
    """
    CREATE TABLE IF NOT EXISTS bank_questions (
        question_id TEXT PRIMARY KEY,
        role_id TEXT NOT NULL,
        role_name TEXT,
        skill TEXT,
        target_level INTEGER,
        question_text TEXT,
        option_a TEXT,
        option_b TEXT,
        option_c TEXT,
        option_d TEXT,
        correct_option TEXT,
        difficulty TEXT,
        is_critical INTEGER,
        ai_confidence REAL,
        sme_status TEXT NOT NULL DEFAULT 'pending' CHECK(sme_status IN ('pending','approved','rejected')),
        reviewed_by TEXT REFERENCES users(user_id),
        reviewed_at TEXT,
        item_json TEXT,
        source TEXT,
        created_by TEXT,
        created_at TEXT,
        times_used INTEGER NOT NULL DEFAULT 0,
        last_used_at TEXT
    );
    """,
    # Progression (certified levels, AI training outlines, promotion
    # nominations). Kept in sync with services/db.py PROGRESSION_SCHEMA.
    """
    CREATE TABLE IF NOT EXISTS skill_certifications (
        cert_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(user_id),
        role_id TEXT,
        skill TEXT NOT NULL,
        level INTEGER NOT NULL CHECK(level BETWEEN 0 AND 5),
        previous_level INTEGER,
        basis TEXT NOT NULL DEFAULT 'certified' CHECK(basis IN ('certified','assessed')),
        cycle_id TEXT REFERENCES assessment_cycles(cycle_id),
        certified_by TEXT REFERENCES users(user_id),
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','revoked')),
        created_at TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS training_outlines (
        outline_id TEXT PRIMARY KEY,
        cycle_id TEXT NOT NULL REFERENCES assessment_cycles(cycle_id),
        user_id TEXT NOT NULL REFERENCES users(user_id),
        mode TEXT NOT NULL CHECK(mode IN ('gap','skill_up')),
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded')),
        items_json TEXT NOT NULL,
        outline_json TEXT,
        event_ids_json TEXT,
        created_by TEXT REFERENCES users(user_id),
        created_at TEXT,
        updated_at TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS promotion_nominations (
        nomination_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(user_id),
        from_role_id TEXT,
        to_role_id TEXT NOT NULL,
        profile_json TEXT NOT NULL,
        manager_id TEXT NOT NULL REFERENCES users(user_id),
        manager_note TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','declined')),
        leader_id TEXT REFERENCES users(user_id),
        leader_note TEXT,
        decided_at TEXT,
        created_at TEXT
    );
    """,
]

# One per role, mirroring the ids/emails the portal's old MOCK_USERS used so
# existing references (manager_id links, demo docs) stay meaningful.
DEMO_USERS = [
    # Manager first -- U001 (below) references M001 as its manager_id, and
    # foreign_keys=ON requires the referenced row to already exist.
    {
        "user_id": "M001", "username": "manager", "email": "manager@talent360.local",
        "full_name": "Manager One", "role": "manager",
        "department": "Finance Operations", "job_title": "Finance Manager",
        "manager_id": None,
    },
    {
        "user_id": "U001", "username": "employee", "email": "employee@talent360.local",
        "full_name": "Employee One", "role": "employee",
        "department": "Finance Operations", "job_title": "P2P Senior Associate",
        "manager_id": "M001",
    },
    {
        "user_id": "S001", "username": "sme", "email": "sme@talent360.local",
        "full_name": "SME Reviewer", "role": "sme",
        "department": "Finance Excellence", "job_title": "Process SME",
        "manager_id": None,
    },
    {
        "user_id": "SC001", "username": "scheduler", "email": "scheduler@talent360.local",
        "full_name": "Assessment Scheduler", "role": "scheduler",
        "department": "People Development", "job_title": "Assessment Scheduler",
        "manager_id": None,
    },
    {
        "user_id": "L001", "username": "leader", "email": "leader@talent360.local",
        "full_name": "Business Leader", "role": "leader",
        "department": "Finance Operations", "job_title": "Function Leader",
        "manager_id": None,
    },
    {
        "user_id": "A001", "username": "admin", "email": "admin@talent360.local",
        "full_name": "System Administrator", "role": "admin",
        "department": "People Systems", "job_title": "Talent Platform Administrator",
        "manager_id": None,
    },
    {
        "user_id": "H001", "username": "hr", "email": "hr@talent360.local",
        "full_name": "HR Partner", "role": "hr",
        "department": "People & Culture", "job_title": "HR Business Partner",
        "manager_id": None,
    },
    # Two more line managers, so the leader's people-by-manager view has teams to compare.
    # scripts/seed_lifecycle.py moves a few synthetic employees to them (once).
    {
        "user_id": "M002", "username": "manager2", "email": "manager2@talent360.local",
        "full_name": "Manager Two", "role": "manager",
        "department": "Order to Cash", "job_title": "O2C Team Manager",
        "manager_id": None,
    },
    {
        "user_id": "M003", "username": "manager3", "email": "manager3@talent360.local",
        "full_name": "Manager Three", "role": "manager",
        "department": "Record to Report", "job_title": "R2R Team Manager",
        "manager_id": None,
    },
]


def build_schema(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    conn.commit()


def _upsert_user(conn: sqlite3.Connection, user: dict, password_hash: str) -> str:
    """Inserts a new user, or -- if the user_id already exists -- only
    resyncs password_hash (role/department/etc. may have been edited since
    via the admin UI, so those are left alone on conflict). Returns
    "inserted" or "password_reset" for the caller's summary counts."""
    existing = conn.execute(
        "SELECT 1 FROM users WHERE user_id = ?", (user["user_id"],)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE user_id = ?",
            (password_hash, user["user_id"]),
        )
        return "password_reset"
    conn.execute(
        """
        INSERT INTO users (user_id, username, email, password_hash, full_name, role, department, job_title, manager_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user["user_id"], user["username"], user["email"], password_hash,
            user["full_name"], user["role"], user["department"], user["job_title"],
            user["manager_id"],
        ),
    )
    return "inserted"


def seed_demo_users(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    password_hash = bcrypt.hashpw(DEMO_PASSWORD.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    results = []
    for user in DEMO_USERS:
        outcome = _upsert_user(conn, user, password_hash)
        results.append((user["username"], outcome))
    conn.commit()
    return results


def load_synthetic_rows():
    wb = openpyxl.load_workbook(DATASET_PATH, read_only=True, data_only=True)
    ws = wb["Users_Teams"]
    rows = list(ws.iter_rows(values_only=True))[3:]  # skip title/blank/header rows
    for row in rows:
        user_id = row[0]
        if not user_id:
            continue
        yield {
            "user_id": user_id,
            "synthetic_name": row[1],
            "tower": row[3],
            "role_name": row[6],
        }


def seed_synthetic_users(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    password_hash = bcrypt.hashpw(SYNTHETIC_PASSWORD.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    results = []
    for row in load_synthetic_rows():
        username = row["user_id"].lower()
        user = {
            "user_id": row["user_id"],
            "username": username,
            "email": f"{username}@talent360.local",
            "full_name": row["synthetic_name"],
            "role": "employee",
            "department": row["tower"],
            "job_title": row["role_name"],
            "manager_id": None,
        }
        outcome = _upsert_user(conn, user, password_hash)
        results.append((username, outcome))
    conn.commit()
    return results


def migrate_user_roles() -> None:
    """A database created before the HR role has a users.role CHECK without 'hr': let the
    portal's own one-time migration widen it before any account is upserted."""
    if not DB_PATH.exists():
        return
    sys.path.insert(0, str(PROJECT_ROOT / "Talent360i_phase11C_fixed_remote_hybrid"))
    from services import db as portal_db  # noqa: E402 -- the portal package, only needed here
    portal_db.DB_PATH = DB_PATH
    portal_db.ensure_user_roles()


def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    migrate_user_roles()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        build_schema(conn)
        demo_results = seed_demo_users(conn)
        synthetic_results = seed_synthetic_users(conn)
    finally:
        conn.close()

    print(f"Users ready at {DB_PATH}\n")
    print("Demo accounts (password = TALENT360_DEMO_PASSWORD):")
    for username, outcome in demo_results:
        print(f"  {username:12s} {outcome}")
    print(f"\nSynthetic dataset accounts (password = TALENT360_SYNTHETIC_PASSWORD), "
          f"{len(synthetic_results)} total:")
    for username, outcome in synthetic_results:
        print(f"  {username:12s} {outcome}")


if __name__ == "__main__":
    main()
