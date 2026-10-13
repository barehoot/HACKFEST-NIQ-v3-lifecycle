"""Data access for the employee-lifecycle module (v3): performance evidence,
reviews, development actions, probation, escalations, the per-employee status
snapshot, job-profile governance, SME coding challenges, notifications and
pulse check-ins.

Same rules as services/db.py: this module is the only writer to these tables,
every value is a bound parameter, and column names that vary come from fixed
allowlists. Tables (and the indexes the 20k-employee views rely on) are
created on first use, once per process per database file, so an existing
database picks them up without a migration step.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from services import db

EVIDENCE_KINDS = ("appreciation", "achievement", "feedback", "one_on_one", "escalation", "concern")
EVIDENCE_SOURCES = ("manual", "outlook", "teams", "email_file", "peer", "integration")
SENTIMENTS = ("positive", "neutral", "negative")
ACTION_TYPES = ("training", "practice", "mentoring", "stretch", "behaviour", "certification", "reading")
ACTION_STATUSES = ("open", "in_progress", "done", "cancelled")
CODING_LANGUAGES = ("python", "sql", "excel", "vba", "javascript", "r", "pseudocode")

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS departments (
        dept_id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        tower TEXT,
        description TEXT,
        leader_id TEXT REFERENCES users(user_id),
        created_by TEXT,
        created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS employee_profiles (
        user_id TEXT PRIMARY KEY REFERENCES users(user_id),
        hire_date TEXT,
        dept_id TEXT REFERENCES departments(dept_id),
        location TEXT,
        employment_type TEXT,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS performance_evidence (
        evidence_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(user_id),
        kind TEXT NOT NULL CHECK(kind IN ('appreciation','achievement','feedback','one_on_one','escalation','concern')),
        source TEXT NOT NULL CHECK(source IN ('manual','outlook','teams','email_file','peer','integration')),
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        occurred_on TEXT NOT NULL,
        sentiment TEXT NOT NULL CHECK(sentiment IN ('positive','neutral','negative')),
        weight INTEGER NOT NULL DEFAULT 1 CHECK(weight BETWEEN 1 AND 3),
        skills_json TEXT,
        status TEXT NOT NULL DEFAULT 'confirmed' CHECK(status IN ('pending','confirmed','dismissed')),
        shared INTEGER NOT NULL DEFAULT 1,
        origin_sender TEXT,
        excerpt TEXT,
        content_hash TEXT,
        ai_json TEXT,
        action_items_json TEXT,
        employee_comment TEXT,
        created_by TEXT REFERENCES users(user_id),
        created_at TEXT,
        decided_by TEXT REFERENCES users(user_id),
        decided_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS performance_reviews (
        review_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(user_id),
        period TEXT NOT NULL,
        rating INTEGER CHECK(rating BETWEEN 1 AND 5),
        summary TEXT,
        strengths TEXT,
        improvements TEXT,
        status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','shared','acknowledged')),
        manager_id TEXT REFERENCES users(user_id),
        ai_assisted INTEGER NOT NULL DEFAULT 0,
        employee_comment TEXT,
        created_at TEXT,
        updated_at TEXT,
        shared_at TEXT,
        acknowledged_at TEXT,
        UNIQUE(user_id, period)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS development_actions (
        action_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(user_id),
        title TEXT NOT NULL,
        detail TEXT,
        action_type TEXT NOT NULL CHECK(action_type IN ('training','practice','mentoring','stretch','behaviour','certification','reading')),
        skill TEXT,
        success_measure TEXT,
        source TEXT NOT NULL CHECK(source IN ('plan','ai','manager','one_on_one','review')),
        due_date TEXT,
        status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','in_progress','done','cancelled')),
        evidence_id TEXT,
        created_by TEXT,
        created_at TEXT,
        updated_at TEXT,
        completed_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS probation_records (
        probation_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(user_id),
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','pending_leader','confirmed','not_confirmed')),
        extensions INTEGER NOT NULL DEFAULT 0,
        recommendation TEXT CHECK(recommendation IN ('confirm','extend','not_confirm')),
        recommendation_note TEXT,
        recommended_by TEXT,
        recommended_at TEXT,
        leader_id TEXT,
        leader_note TEXT,
        decided_at TEXT,
        history_json TEXT,
        created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS probation_checkpoints (
        checkpoint_id TEXT PRIMARY KEY,
        probation_id TEXT NOT NULL REFERENCES probation_records(probation_id),
        label TEXT NOT NULL,
        due_date TEXT NOT NULL,
        rating TEXT CHECK(rating IN ('on_track','at_risk','off_track')),
        note TEXT,
        completed_by TEXT,
        completed_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS performance_escalations (
        escalation_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(user_id),
        level TEXT NOT NULL CHECK(level IN ('manager','leader')),
        reason TEXT NOT NULL,
        suggestion TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','acknowledged','resolved')),
        handled_by TEXT,
        note TEXT,
        created_at TEXT,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS performance_insights (
        insight_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(user_id),
        insight_json TEXT NOT NULL,
        generated_by TEXT,
        requested_by TEXT,
        created_at TEXT
    )
    """,
    # Read model: one row per employee, refreshed on every write that affects
    # them and by the lifecycle worker. Org-wide views (20k+ people) page
    # through this table instead of recomputing everyone on each request.
    """
    CREATE TABLE IF NOT EXISTS employee_status (
        user_id TEXT PRIMARY KEY,
        full_name TEXT,
        manager_id TEXT,
        dept_id TEXT,
        department TEXT,
        role_id TEXT,
        role_name TEXT,
        tower TEXT,
        band TEXT NOT NULL,
        score INTEGER,
        readiness_pct INTEGER,
        promotion_level TEXT,
        signals_json TEXT,
        factors_json TEXT,
        band_since TEXT,
        computed_at TEXT NOT NULL,
        attention_rank INTEGER
    )
    """,
    # One row per (employee, signal): the signal filter is an index lookup, not a JSON scan.
    """
    CREATE TABLE IF NOT EXISTS employee_signals (
        user_id TEXT NOT NULL,
        signal TEXT NOT NULL,
        PRIMARY KEY (user_id, signal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_profiles (
        role_id TEXT PRIMARY KEY,
        dept_id TEXT REFERENCES departments(dept_id),
        description TEXT,
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','retired')),
        created_by TEXT,
        created_at TEXT,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS skill_requirement_changes (
        change_id TEXT PRIMARY KEY,
        role_id TEXT NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('add','update','remove')),
        skill TEXT NOT NULL,
        payload_json TEXT,
        reason TEXT NOT NULL,
        trend_id TEXT,
        status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','applied','rejected')),
        proposed_by TEXT,
        proposed_at TEXT,
        decided_by TEXT,
        decided_at TEXT,
        decision_note TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS skill_trends (
        trend_id TEXT PRIMARY KEY,
        skill TEXT NOT NULL,
        tower TEXT,
        direction TEXT NOT NULL CHECK(direction IN ('emerging','rising','stable','declining')),
        note TEXT,
        source TEXT,
        added_by TEXT,
        created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS coding_challenges (
        challenge_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        prompt TEXT NOT NULL,
        language TEXT NOT NULL CHECK(language IN ('python','sql','excel','vba','javascript','r','pseudocode')),
        starter_code TEXT,
        keywords_json TEXT NOT NULL,
        skills_json TEXT,
        difficulty TEXT NOT NULL DEFAULT 'intermediate' CHECK(difficulty IN ('foundational','intermediate','advanced')),
        rubric TEXT,
        time_minutes INTEGER,
        status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','approved','retired')),
        created_by TEXT,
        created_at TEXT,
        approved_by TEXT,
        approved_at TEXT
    )
    """,
    # Only the SME-chosen keywords found in the resume are kept -- never the resume.
    """
    CREATE TABLE IF NOT EXISTS cycle_resume_signals (
        cycle_id TEXT PRIMARY KEY,
        keywords_json TEXT NOT NULL,
        created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS coding_attempts (
        attempt_id TEXT PRIMARY KEY,
        cycle_id TEXT NOT NULL,
        challenge_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        language TEXT,
        code TEXT NOT NULL,
        paste_chars INTEGER,
        submitted_at TEXT,
        review_json TEXT,
        review_status TEXT NOT NULL DEFAULT 'pending_review' CHECK(review_status IN ('ai_reviewed','pending_review','sme_reviewed')),
        sme_score INTEGER CHECK(sme_score BETWEEN 1 AND 5),
        sme_note TEXT,
        reviewed_by TEXT,
        reviewed_at TEXT,
        UNIQUE(cycle_id, challenge_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notifications (
        notification_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT,
        link TEXT,
        channels TEXT,
        read_at TEXT,
        created_at TEXT
    )
    """,
    # A vertical (tower) is the top of the organisation tree: verticals -> departments -> job profiles.
    """
    CREATE TABLE IF NOT EXISTS verticals (
        code TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        description TEXT,
        created_by TEXT,
        created_at TEXT
    )
    """,
    # Agent 8 suggestions waiting for a person: skills a job profile should require
    # (scope 'role') or new skills for the catalogue (scope 'catalogue').
    """
    CREATE TABLE IF NOT EXISTS skill_suggestions (
        suggestion_id TEXT PRIMARY KEY,
        batch_id TEXT NOT NULL,
        scope TEXT NOT NULL CHECK(scope IN ('role','catalogue')),
        role_id TEXT,
        tower TEXT,
        skill TEXT NOT NULL,
        existing INTEGER NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL,
        rationale TEXT,
        generated_by TEXT,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','proposed','dismissed')),
        created_by TEXT,
        created_at TEXT,
        decided_by TEXT,
        decided_at TEXT
    )
    """,
    # Agent 9 summaries for a mid-year / annual review, a 1:1 or a promotion discussion,
    # with the deterministic facts they were built from (shown next to them).
    """
    CREATE TABLE IF NOT EXISTS performance_summaries (
        summary_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        review_type TEXT NOT NULL CHECK(review_type IN ('mid_year','annual','one_on_one','promotion')),
        target_role_id TEXT,
        window_from TEXT,
        window_to TEXT,
        facts_json TEXT NOT NULL,
        summary_json TEXT NOT NULL,
        generated_by TEXT,
        requested_by TEXT,
        created_at TEXT
    )
    """,
    # Career mobility: what an employee aspires to (any job profile, any vertical, or an
    # internal posting), skills their CV claims, internal postings, applications, shortlists.
    """
    CREATE TABLE IF NOT EXISTS career_aspirations (
        aspiration_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        role_id TEXT,
        posting_id TEXT,
        note TEXT,
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','withdrawn')),
        created_at TEXT,
        updated_at TEXT
    )
    """,
    # Only the claim and a short verbatim quote are kept -- never the CV.
    """
    CREATE TABLE IF NOT EXISTS cv_skill_claims (
        claim_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        skill TEXT NOT NULL,
        level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 5),
        quote TEXT,
        generated_by TEXT,
        source_file TEXT,
        created_at TEXT,
        UNIQUE(user_id, skill)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_postings (
        posting_id TEXT PRIMARY KEY,
        source TEXT NOT NULL CHECK(source IN ('smartrecruiters','demo')),
        title TEXT NOT NULL,
        department TEXT,
        location TEXT,
        experience_level TEXT,
        employment_type TEXT,
        released_on TEXT,
        apply_url TEXT,
        description TEXT,
        qualifications TEXT,
        role_id TEXT,
        mapped_by TEXT,
        hiring_manager_id TEXT,
        status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
        fetched_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS internal_applications (
        application_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        role_id TEXT,
        posting_id TEXT,
        note TEXT,
        readiness_json TEXT,
        status TEXT NOT NULL DEFAULT 'submitted' CHECK(status IN ('submitted','shortlisted','declined','withdrawn')),
        decided_by TEXT,
        decision_note TEXT,
        created_at TEXT,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS talent_shortlists (
        shortlist_id TEXT PRIMARY KEY,
        role_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        added_by TEXT NOT NULL,
        note TEXT,
        created_at TEXT,
        UNIQUE(role_id, user_id, added_by)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pulse_checks (
        pulse_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(user_id),
        week TEXT NOT NULL,
        mood INTEGER NOT NULL CHECK(mood BETWEEN 1 AND 5),
        workload INTEGER NOT NULL CHECK(workload BETWEEN 1 AND 5),
        comment TEXT,
        shared INTEGER NOT NULL DEFAULT 0,
        created_at TEXT,
        UNIQUE(user_id, week)
    )
    """,
)

# Indexes for the paths that grow with headcount. Tables that only exist once
# another module first touches them are skipped until then (OperationalError).
INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_evidence_user ON performance_evidence(user_id, status, occurred_on)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_hash ON performance_evidence(user_id, content_hash) WHERE content_hash IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_reviews_user ON performance_reviews(user_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_actions_user ON development_actions(user_id, status, due_date)",
    "CREATE INDEX IF NOT EXISTS idx_probation_user ON probation_records(user_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_checkpoints_probation ON probation_checkpoints(probation_id)",
    "CREATE INDEX IF NOT EXISTS idx_escalations_user ON performance_escalations(user_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_insights_user ON performance_insights(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_status_band ON employee_status(band, attention_rank)",
    "CREATE INDEX IF NOT EXISTS idx_status_attention ON employee_status(attention_rank, full_name)",
    "CREATE INDEX IF NOT EXISTS idx_status_manager ON employee_status(manager_id, attention_rank)",
    "CREATE INDEX IF NOT EXISTS idx_status_dept ON employee_status(dept_id, attention_rank)",
    "CREATE INDEX IF NOT EXISTS idx_status_name ON employee_status(full_name)",
    "CREATE INDEX IF NOT EXISTS idx_status_dept_band ON employee_status(department, band)",
    "CREATE INDEX IF NOT EXISTS idx_status_promotion ON employee_status(promotion_level, band)",
    "CREATE INDEX IF NOT EXISTS idx_signals_signal ON employee_signals(signal, user_id)",
    "CREATE INDEX IF NOT EXISTS idx_changes_role ON skill_requirement_changes(role_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_suggestions_scope ON skill_suggestions(scope, status, role_id)",
    "CREATE INDEX IF NOT EXISTS idx_summaries_user ON performance_summaries(user_id, review_type, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_aspirations_user ON career_aspirations(user_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_aspirations_role ON career_aspirations(role_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_applications_role ON internal_applications(role_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_applications_user ON internal_applications(user_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_postings_role ON job_postings(role_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_certs_skill ON skill_certifications(lower(skill))",
    "CREATE INDEX IF NOT EXISTS idx_gaps_skill ON skill_gaps(lower(skill))",
    "CREATE INDEX IF NOT EXISTS idx_attempts_cycle ON coding_attempts(cycle_id)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, read_at, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_dedupe ON notifications(user_id, kind, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_pulse_user ON pulse_checks(user_id, week)",
    "CREATE INDEX IF NOT EXISTS idx_users_manager ON users(manager_id)",
    "CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)",
    "CREATE INDEX IF NOT EXISTS idx_cycles_user ON assessment_cycles(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_cycles_status ON assessment_cycles(status)",
    "CREATE INDEX IF NOT EXISTS idx_gq_cycle ON generated_questions(cycle_id)",
    "CREATE INDEX IF NOT EXISTS idx_gaps_user ON skill_gaps(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_rsm_role ON role_skill_map(role_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_user ON scheduled_events(user_id, event_date)",
    "CREATE INDEX IF NOT EXISTS idx_certs_user ON skill_certifications(user_id, status)",
)

_READY: set[str] = set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


def _rows(rows) -> list[dict]:
    return [dict(r) for r in rows]


def ensure_schema() -> None:
    key = str(db.DB_PATH)
    if key in _READY:
        return
    conn = db.get_conn()
    try:
        for statement in SCHEMA:
            conn.execute(statement)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(employee_status)")}
        if "attention_rank" not in columns:  # snapshot created by an earlier v3 build
            conn.execute("ALTER TABLE employee_status ADD COLUMN attention_rank INTEGER")
        for statement in INDEXES:
            try:
                conn.execute(statement)
            except sqlite3.OperationalError:
                pass  # table not created yet; the index follows on a later process start
        _seed_verticals(conn)
        conn.commit()
    finally:
        conn.close()
    _READY.add(key)


DEFAULT_VERTICALS = (("AP", "Accounts Payable", "Invoice-to-pay, T&E and supplier operations"),
                     ("O2C", "Order to Cash", "Billing, revenue assurance and cash application"),
                     ("R2R", "Record to Report", "Banking, GA, consolidation and close"))


def _seed_verticals(conn: sqlite3.Connection) -> None:
    """The three dataset towers, plus any tower the role catalogue already uses."""
    for code, name, description in DEFAULT_VERTICALS:
        conn.execute("INSERT OR IGNORE INTO verticals (code, name, description, created_by, created_at) "
                     "VALUES (?, ?, ?, 'seed', ?)", (code, name, description, _now()))
    try:
        towers = [r[0] for r in conn.execute("SELECT DISTINCT tower FROM role_master WHERE tower IS NOT NULL AND tower != ''")]
    except sqlite3.OperationalError:
        towers = []  # no reference data yet
    for tower in towers:
        conn.execute("INSERT OR IGNORE INTO verticals (code, name, description, created_by, created_at) "
                     "VALUES (?, ?, '', 'seed', ?)", (tower, tower, _now()))


def conn() -> sqlite3.Connection:
    ensure_schema()
    return db.get_conn()


def _ids_param(user_ids) -> str:
    # The id list travels as one JSON parameter, so the SQL text stays fixed.
    return json.dumps(list(user_ids))


# ---------------------------------------------------------------------------
# Bulk readers over the core tables (the status engine scores people in batches)
# ---------------------------------------------------------------------------

def users_by_ids(user_ids) -> list[dict]:
    c = conn()
    try:
        return _rows(c.execute(
            "SELECT user_id, full_name, email, role, job_title, department, manager_id FROM users "
            "WHERE user_id IN (SELECT value FROM json_each(?)) ORDER BY user_id", (_ids_param(user_ids),)).fetchall())
    finally:
        c.close()


def user_ids_with_role(role: str) -> list[str]:
    c = conn()
    try:
        return [r[0] for r in c.execute("SELECT user_id FROM users WHERE role = ? ORDER BY user_id", (role,)).fetchall()]
    finally:
        c.close()


def user_by_email(email: str) -> dict | None:
    c = conn()
    try:
        row = c.execute("SELECT user_id, full_name, email, role, manager_id FROM users WHERE lower(email) = lower(?)",
                        (email,)).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def count_with_title(title: str) -> int:
    c = conn()
    try:
        return c.execute("SELECT COUNT(*) FROM users WHERE role = 'employee' AND lower(job_title) = lower(?)",
                         (title,)).fetchone()[0]
    finally:
        c.close()


def headcount() -> dict:
    c = conn()
    try:
        row = c.execute("SELECT COUNT(*), SUM(manager_id IS NULL), "
                        "(SELECT COUNT(*) FROM users WHERE role = 'manager') FROM users WHERE role = 'employee'").fetchone()
        return {"employees": row[0] or 0, "unassigned": row[1] or 0, "managers": row[2] or 0}
    finally:
        c.close()


def employee_ids(manager_id: str | None = None) -> list[str]:
    c = conn()
    try:
        return [r[0] for r in c.execute(
            "SELECT user_id FROM users WHERE role = 'employee' AND (? IS NULL OR manager_id = ?) ORDER BY user_id",
            (manager_id, manager_id)).fetchall()]
    finally:
        c.close()


def cycles_for_users(user_ids) -> dict[str, list[dict]]:
    """Assessment cycles per employee, newest first, with the score/evidence fields the engine reads."""
    c = conn()
    try:
        out: dict[str, list[dict]] = {}
        for r in c.execute(
            "SELECT cycle_id, user_id, status, manager_decision, matched_role_id, matched_role_name, mcq_score_json, "
            "evidence_report_json, created_at, updated_at FROM assessment_cycles "
            "WHERE user_id IN (SELECT value FROM json_each(?)) ORDER BY created_at DESC, rowid DESC",
                (_ids_param(user_ids),)).fetchall():
            row = dict(r)
            score = json.loads(row.pop("mcq_score_json") or "null") or {}
            evidence = json.loads(row.pop("evidence_report_json") or "null") or {}
            row.update(mcq_pct=score.get("overall_score_pct"), evidence_pct=evidence.get("evidence_score_pct"),
                       integrity_risk=evidence.get("integrity_risk"), band=evidence.get("compatibility_band"))
            out.setdefault(row["user_id"], []).append(row)
        return out
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Departments and employee profiles (HR attributes; Workday-synced in production)
# ---------------------------------------------------------------------------

def list_departments() -> list[dict]:
    c = conn()
    try:
        return _rows(c.execute(
            """
            SELECT d.*, u.full_name AS leader_name,
                   (SELECT COUNT(*) FROM employee_profiles p WHERE p.dept_id = d.dept_id) AS people,
                   (SELECT COUNT(*) FROM job_profiles j WHERE j.dept_id = d.dept_id) AS roles
            FROM departments d LEFT JOIN users u ON u.user_id = d.leader_id
            ORDER BY d.name
            """).fetchall())
    finally:
        c.close()


def get_department(dept_id: str) -> dict | None:
    c = conn()
    try:
        row = c.execute("SELECT * FROM departments WHERE dept_id = ?", (dept_id,)).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def create_department(*, name: str, tower: str | None, description: str, leader_id: str | None, created_by: str) -> str | None:
    """None when a department with that name already exists."""
    c = conn()
    try:
        dept_id = _id("DEPT")
        c.execute("INSERT INTO departments (dept_id, name, tower, description, leader_id, created_by, created_at) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?)", (dept_id, name, tower or None, description, leader_id or None, created_by, _now()))
        c.commit()
        return dept_id
    except sqlite3.IntegrityError:
        return None
    finally:
        c.close()


def departments_led_by(leader_id: str) -> list[str]:
    c = conn()
    try:
        return [r[0] for r in c.execute("SELECT dept_id FROM departments WHERE leader_id = ?", (leader_id,)).fetchall()]
    finally:
        c.close()


def get_profile(user_id: str) -> dict | None:
    c = conn()
    try:
        row = c.execute("SELECT p.*, d.name AS department_name FROM employee_profiles p "
                        "LEFT JOIN departments d ON d.dept_id = p.dept_id WHERE p.user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def profiles_for(user_ids) -> dict[str, dict]:
    c = conn()
    try:
        return {r["user_id"]: dict(r) for r in c.execute(
            "SELECT p.*, d.name AS department_name FROM employee_profiles p LEFT JOIN departments d ON d.dept_id = p.dept_id "
            "WHERE p.user_id IN (SELECT value FROM json_each(?))", (_ids_param(user_ids),)).fetchall()}
    finally:
        c.close()


def upsert_profile(user_id: str, *, hire_date: str | None = None, dept_id: str | None = None,
                   location: str | None = None, employment_type: str | None = None) -> None:
    c = conn()
    try:
        c.execute(
            """
            INSERT INTO employee_profiles (user_id, hire_date, dept_id, location, employment_type, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                hire_date = COALESCE(excluded.hire_date, hire_date), dept_id = COALESCE(excluded.dept_id, dept_id),
                location = COALESCE(excluded.location, location),
                employment_type = COALESCE(excluded.employment_type, employment_type), updated_at = excluded.updated_at
            """,
            (user_id, hire_date, dept_id, location, employment_type, _now()))
        c.commit()
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Performance evidence
# ---------------------------------------------------------------------------

def _parse_evidence(row) -> dict:
    item = dict(row)
    item["skills"] = json.loads(item["skills_json"]) if item.get("skills_json") else []
    item["ai"] = json.loads(item["ai_json"]) if item.get("ai_json") else None
    item["action_items"] = json.loads(item["action_items_json"]) if item.get("action_items_json") else []
    return item


def add_evidence(*, user_id: str, kind: str, source: str, title: str, summary: str, occurred_on: str,
                 sentiment: str, created_by: str, weight: int = 1, skills: list[str] | None = None,
                 status: str = "confirmed", shared: bool = True, origin_sender: str | None = None,
                 excerpt: str | None = None, content_hash: str | None = None, ai: dict | None = None,
                 action_items: list[str] | None = None) -> str | None:
    """None when the same message was already tagged to this employee (content hash)."""
    evidence_id = _id("EVD")
    c = conn()
    try:
        c.execute(
            """
            INSERT INTO performance_evidence
                (evidence_id, user_id, kind, source, title, summary, occurred_on, sentiment, weight, skills_json, status,
                 shared, origin_sender, excerpt, content_hash, ai_json, action_items_json, created_by, created_at,
                 decided_by, decided_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (evidence_id, user_id, kind, source, title, summary, occurred_on, sentiment, max(1, min(3, int(weight))),
             json.dumps(skills or []), status, 1 if shared else 0, origin_sender, excerpt, content_hash,
             json.dumps(ai) if ai else None, json.dumps(action_items or []), created_by, _now(),
             created_by if status == "confirmed" else None, _now() if status == "confirmed" else None))
        c.commit()
        return evidence_id
    except sqlite3.IntegrityError:
        return None
    finally:
        c.close()


def set_evidence_ai(evidence_id: str, ai: dict) -> None:
    """Agent 7's reading of a note the manager wrote: context only, the record itself is unchanged."""
    c = conn()
    try:
        c.execute("UPDATE performance_evidence SET ai_json = ? WHERE evidence_id = ?", (json.dumps(ai), evidence_id))
        c.commit()
    finally:
        c.close()


def get_evidence(evidence_id: str) -> dict | None:
    c = conn()
    try:
        row = c.execute("SELECT e.*, u.full_name AS created_by_name FROM performance_evidence e "
                        "LEFT JOIN users u ON u.user_id = e.created_by WHERE e.evidence_id = ?", (evidence_id,)).fetchone()
        return _parse_evidence(row) if row else None
    finally:
        c.close()


def list_evidence(user_id: str, *, statuses=("confirmed",), shared_only: bool = False, limit: int = 200) -> list[dict]:
    c = conn()
    try:
        rows = c.execute(
            """
            SELECT e.*, u.full_name AS created_by_name FROM performance_evidence e
            LEFT JOIN users u ON u.user_id = e.created_by
            WHERE e.user_id = ? AND e.status IN (SELECT value FROM json_each(?)) AND (? = 0 OR e.shared = 1)
            ORDER BY e.occurred_on DESC, e.created_at DESC LIMIT ?
            """, (user_id, json.dumps(list(statuses)), 1 if shared_only else 0, limit)).fetchall()
        return [_parse_evidence(r) for r in rows]
    finally:
        c.close()


def evidence_for_users(user_ids, since: str) -> dict[str, list[dict]]:
    """Confirmed evidence on or after `since` for many employees (status engine, one query)."""
    c = conn()
    try:
        out: dict[str, list[dict]] = {}
        for r in c.execute(
            "SELECT evidence_id, user_id, kind, sentiment, weight, shared, occurred_on, title FROM performance_evidence "
            "WHERE user_id IN (SELECT value FROM json_each(?)) AND status = 'confirmed' AND occurred_on >= ?",
                (_ids_param(user_ids), since)).fetchall():
            out.setdefault(r["user_id"], []).append(dict(r))
        return out
    finally:
        c.close()


_EVIDENCE_EDITABLE = ("kind", "sentiment", "title", "summary", "occurred_on", "weight", "skills_json", "shared")


def decide_evidence(evidence_id: str, status: str, decided_by: str, **updates) -> bool:
    """Pending -> confirmed/dismissed, applying the manager's edits to the AI suggestion."""
    fields = {k: v for k, v in updates.items() if k in _EVIDENCE_EDITABLE and v is not None}
    sets = "".join(f", {k} = ?" for k in fields)
    c = conn()
    try:
        # Allowlisted column names, bound values (bandit B608 reviewed).
        cur = c.execute(
            f"UPDATE performance_evidence SET status = ?, decided_by = ?, decided_at = ?{sets} "  # nosec B608
            "WHERE evidence_id = ? AND status = 'pending'",
            (status, decided_by, _now(), *fields.values(), evidence_id))
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def retract_evidence(evidence_id: str, decided_by: str) -> bool:
    c = conn()
    try:
        cur = c.execute("UPDATE performance_evidence SET status = 'dismissed', decided_by = ?, decided_at = ? "
                        "WHERE evidence_id = ? AND status = 'confirmed'", (decided_by, _now(), evidence_id))
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def set_evidence_comment(evidence_id: str, user_id: str, comment: str) -> bool:
    c = conn()
    try:
        cur = c.execute("UPDATE performance_evidence SET employee_comment = ? "
                        "WHERE evidence_id = ? AND user_id = ? AND status = 'confirmed' AND shared = 1",
                        (comment, evidence_id, user_id))
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def pending_evidence(user_ids) -> list[dict]:
    c = conn()
    try:
        rows = c.execute(
            """
            SELECT e.*, u.full_name AS employee_name FROM performance_evidence e JOIN users u ON u.user_id = e.user_id
            WHERE e.user_id IN (SELECT value FROM json_each(?)) AND e.status = 'pending'
            ORDER BY e.created_at
            """, (_ids_param(user_ids),)).fetchall()
        return [_parse_evidence(r) for r in rows]
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Performance reviews
# ---------------------------------------------------------------------------

def save_review(*, user_id: str, period: str, manager_id: str, rating: int | None, summary: str, strengths: str,
                improvements: str, share: bool, ai_assisted: bool) -> str | None:
    """Creates or updates the period's review. None if it was already acknowledged (locked)."""
    c = conn()
    try:
        existing = c.execute("SELECT review_id, status FROM performance_reviews WHERE user_id = ? AND period = ?",
                             (user_id, period)).fetchone()
        if existing and existing["status"] == "acknowledged":
            return None
        status = "shared" if share else (existing["status"] if existing else "draft")
        if existing:
            c.execute(
                """
                UPDATE performance_reviews SET rating = ?, summary = ?, strengths = ?, improvements = ?, status = ?,
                       manager_id = ?, ai_assisted = MAX(ai_assisted, ?), updated_at = ?,
                       shared_at = CASE WHEN ? = 'shared' AND shared_at IS NULL THEN ? ELSE shared_at END
                WHERE review_id = ?
                """, (rating, summary, strengths, improvements, status, manager_id, 1 if ai_assisted else 0, _now(),
                      status, _now(), existing["review_id"]))
            review_id = existing["review_id"]
        else:
            review_id = _id("REV")
            c.execute(
                """
                INSERT INTO performance_reviews (review_id, user_id, period, rating, summary, strengths, improvements, status,
                                                 manager_id, ai_assisted, created_at, updated_at, shared_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (review_id, user_id, period, rating, summary, strengths, improvements, status, manager_id,
                      1 if ai_assisted else 0, _now(), _now(), _now() if status == "shared" else None))
        c.commit()
        return review_id
    finally:
        c.close()


def list_reviews(user_id: str, *, include_drafts: bool = True) -> list[dict]:
    c = conn()
    try:
        return _rows(c.execute(
            "SELECT r.*, u.full_name AS manager_name FROM performance_reviews r LEFT JOIN users u ON u.user_id = r.manager_id "
            "WHERE r.user_id = ? AND (? OR r.status != 'draft') ORDER BY r.period DESC",
            (user_id, 1 if include_drafts else 0)).fetchall())
    finally:
        c.close()


def get_review(review_id: str) -> dict | None:
    c = conn()
    try:
        row = c.execute("SELECT * FROM performance_reviews WHERE review_id = ?", (review_id,)).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def acknowledge_review(review_id: str, user_id: str, comment: str) -> bool:
    c = conn()
    try:
        cur = c.execute("UPDATE performance_reviews SET status = 'acknowledged', employee_comment = ?, acknowledged_at = ? "
                        "WHERE review_id = ? AND user_id = ? AND status = 'shared'", (comment, _now(), review_id, user_id))
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def latest_reviews(user_ids) -> dict[str, dict]:
    """Newest shared or acknowledged review per employee (drafts never count)."""
    c = conn()
    try:
        out = {}
        for r in c.execute(
            "SELECT * FROM performance_reviews WHERE user_id IN (SELECT value FROM json_each(?)) "
            "AND status != 'draft' ORDER BY period DESC, updated_at DESC", (_ids_param(user_ids),)).fetchall():
            out.setdefault(r["user_id"], dict(r))
        return out
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Development actions (the individual development plan)
# ---------------------------------------------------------------------------

def add_actions(user_id: str, actions: list[dict], created_by: str) -> list[str]:
    c = conn()
    try:
        ids = []
        for a in actions:
            action_id = _id("ACT")
            c.execute(
                """
                INSERT INTO development_actions (action_id, user_id, title, detail, action_type, skill, success_measure, source,
                                                 due_date, status, evidence_id, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
                """, (action_id, user_id, a["title"], a.get("detail"), a["action_type"], a.get("skill"),
                      a.get("success_measure"), a.get("source", "manager"), a.get("due_date"), a.get("evidence_id"),
                      created_by, _now(), _now()))
            ids.append(action_id)
        c.commit()
        return ids
    finally:
        c.close()


def list_actions(user_id: str, *, include_closed: bool = True) -> list[dict]:
    c = conn()
    try:
        return _rows(c.execute(
            "SELECT * FROM development_actions WHERE user_id = ? AND (? OR status IN ('open','in_progress')) "
            "ORDER BY CASE status WHEN 'in_progress' THEN 0 WHEN 'open' THEN 1 WHEN 'done' THEN 2 ELSE 3 END, "
            "COALESCE(due_date, '9999'), created_at", (user_id, 1 if include_closed else 0)).fetchall())
    finally:
        c.close()


def get_action(action_id: str) -> dict | None:
    c = conn()
    try:
        row = c.execute("SELECT * FROM development_actions WHERE action_id = ?", (action_id,)).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def set_action_status(action_id: str, status: str) -> bool:
    c = conn()
    try:
        cur = c.execute(
            "UPDATE development_actions SET status = ?, updated_at = ?, "
            "completed_at = CASE WHEN ? = 'done' THEN ? ELSE NULL END WHERE action_id = ? AND status != 'cancelled'",
            (status, _now(), status, _now(), action_id))
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def actions_for_users(user_ids) -> dict[str, list[dict]]:
    c = conn()
    try:
        out: dict[str, list[dict]] = {}
        for r in c.execute("SELECT action_id, user_id, title, action_type, skill, status, due_date, created_at "
                           "FROM development_actions WHERE user_id IN (SELECT value FROM json_each(?)) AND status != 'cancelled'",
                           (_ids_param(user_ids),)).fetchall():
            out.setdefault(r["user_id"], []).append(dict(r))
        return out
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Probation
# ---------------------------------------------------------------------------

def _parse_probation(row) -> dict:
    item = dict(row)
    item["history"] = json.loads(item["history_json"]) if item.get("history_json") else []
    return item


def create_probation(user_id: str, start_date: str, end_date: str, checkpoints: list[tuple[str, str]]) -> str | None:
    """None when the employee already has a probation record."""
    c = conn()
    try:
        if c.execute("SELECT 1 FROM probation_records WHERE user_id = ?", (user_id,)).fetchone():
            return None
        probation_id = _id("PRB")
        c.execute("INSERT INTO probation_records (probation_id, user_id, start_date, end_date, history_json, created_at) "
                  "VALUES (?, ?, ?, ?, '[]', ?)", (probation_id, user_id, start_date, end_date, _now()))
        for label, due in checkpoints:
            c.execute("INSERT INTO probation_checkpoints (checkpoint_id, probation_id, label, due_date) VALUES (?, ?, ?, ?)",
                      (_id("PCK"), probation_id, label, due))
        c.commit()
        return probation_id
    finally:
        c.close()


def new_joiners_without_probation(since: str, until: str) -> list[tuple[str, str]]:
    """(user_id, hire_date) for employees hired in [since, until] with no probation record."""
    c = conn()
    try:
        return [(r[0], r[1]) for r in c.execute(
            "SELECT p.user_id, p.hire_date FROM employee_profiles p JOIN users u ON u.user_id = p.user_id "
            "WHERE u.role = 'employee' AND p.hire_date BETWEEN ? AND ? "
            "AND NOT EXISTS (SELECT 1 FROM probation_records r WHERE r.user_id = p.user_id)", (since, until)).fetchall()]
    finally:
        c.close()


def get_probation(probation_id: str) -> dict | None:
    c = conn()
    try:
        row = c.execute("SELECT * FROM probation_records WHERE probation_id = ?", (probation_id,)).fetchone()
        return _parse_probation(row) if row else None
    finally:
        c.close()


def probation_for_user(user_id: str) -> dict | None:
    c = conn()
    try:
        row = c.execute("SELECT * FROM probation_records WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
                        (user_id,)).fetchone()
        return _parse_probation(row) if row else None
    finally:
        c.close()


def probations_for_users(user_ids) -> dict[str, dict]:
    c = conn()
    try:
        return {r["user_id"]: _parse_probation(r) for r in c.execute(
            "SELECT * FROM probation_records WHERE user_id IN (SELECT value FROM json_each(?))",
            (_ids_param(user_ids),)).fetchall()}
    finally:
        c.close()


def list_probations(*, statuses=None) -> list[dict]:
    c = conn()
    try:
        rows = c.execute(
            """
            SELECT p.*, u.full_name AS employee_name, u.job_title, u.manager_id, m.full_name AS manager_name
            FROM probation_records p JOIN users u ON u.user_id = p.user_id LEFT JOIN users m ON m.user_id = u.manager_id
            WHERE (? IS NULL OR p.status IN (SELECT value FROM json_each(?)))
            ORDER BY CASE p.status WHEN 'pending_leader' THEN 0 WHEN 'active' THEN 1 ELSE 2 END, p.end_date
            """, (None if statuses is None else 1, json.dumps(list(statuses or [])))).fetchall()
        return [_parse_probation(r) for r in rows]
    finally:
        c.close()


def checkpoints(probation_id: str) -> list[dict]:
    c = conn()
    try:
        return _rows(c.execute(
            "SELECT k.*, u.full_name AS completed_by_name FROM probation_checkpoints k "
            "LEFT JOIN users u ON u.user_id = k.completed_by WHERE k.probation_id = ? ORDER BY k.due_date",
            (probation_id,)).fetchall())
    finally:
        c.close()


def checkpoints_for(probation_ids) -> dict[str, list[dict]]:
    c = conn()
    try:
        out: dict[str, list[dict]] = {}
        for r in c.execute("SELECT * FROM probation_checkpoints WHERE probation_id IN (SELECT value FROM json_each(?)) "
                           "ORDER BY due_date", (_ids_param(probation_ids),)).fetchall():
            out.setdefault(r["probation_id"], []).append(dict(r))
        return out
    finally:
        c.close()


def complete_checkpoint(checkpoint_id: str, probation_id: str, rating: str, note: str, by: str) -> bool:
    c = conn()
    try:
        cur = c.execute("UPDATE probation_checkpoints SET rating = ?, note = ?, completed_by = ?, completed_at = ? "
                        "WHERE checkpoint_id = ? AND probation_id = ?", (rating, note, by, _now(), checkpoint_id, probation_id))
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def add_checkpoint(probation_id: str, label: str, due_date: str) -> None:
    c = conn()
    try:
        c.execute("INSERT INTO probation_checkpoints (checkpoint_id, probation_id, label, due_date) VALUES (?, ?, ?, ?)",
                  (_id("PCK"), probation_id, label, due_date))
        c.commit()
    finally:
        c.close()


def _append_history(c, probation_id: str, entry: dict) -> None:
    row = c.execute("SELECT history_json FROM probation_records WHERE probation_id = ?", (probation_id,)).fetchone()
    history = json.loads(row["history_json"]) if row and row["history_json"] else []
    history.append({**entry, "at": _now()})
    c.execute("UPDATE probation_records SET history_json = ? WHERE probation_id = ?", (json.dumps(history), probation_id))


def recommend_probation(probation_id: str, recommendation: str, note: str, by: str) -> bool:
    c = conn()
    try:
        cur = c.execute(
            "UPDATE probation_records SET status = 'pending_leader', recommendation = ?, recommendation_note = ?, "
            "recommended_by = ?, recommended_at = ? WHERE probation_id = ? AND status = 'active'",
            (recommendation, note, by, _now(), probation_id))
        if cur.rowcount:
            _append_history(c, probation_id, {"event": "recommended", "recommendation": recommendation, "by": by, "note": note})
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def decide_probation(probation_id: str, *, status: str, leader_id: str, note: str, end_date: str | None = None,
                     event: str) -> bool:
    """Leader decision on a pending recommendation: final status, or back to active (returned / extended)."""
    c = conn()
    try:
        cur = c.execute(
            """
            UPDATE probation_records SET status = ?, leader_id = ?, leader_note = ?, decided_at = ?,
                   end_date = COALESCE(?, end_date),
                   extensions = extensions + CASE WHEN ? = 'extended' THEN 1 ELSE 0 END,
                   recommendation = CASE WHEN ? = 'active' THEN NULL ELSE recommendation END
            WHERE probation_id = ? AND status = 'pending_leader'
            """, (status, leader_id, note, _now(), end_date, event, status, probation_id))
        if cur.rowcount:
            _append_history(c, probation_id, {"event": event, "by": leader_id, "note": note})
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Escalations and AI insights
# ---------------------------------------------------------------------------

def open_escalation(user_id: str, level: str, reason: str, suggestion: str) -> str | None:
    """Idempotent: None while an escalation at this level is still open or acknowledged."""
    c = conn()
    try:
        if c.execute("SELECT 1 FROM performance_escalations WHERE user_id = ? AND level = ? AND status != 'resolved'",
                     (user_id, level)).fetchone():
            return None
        escalation_id = _id("ESC")
        c.execute("INSERT INTO performance_escalations (escalation_id, user_id, level, reason, suggestion, created_at, updated_at) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?)", (escalation_id, user_id, level, reason, suggestion, _now(), _now()))
        c.commit()
        return escalation_id
    finally:
        c.close()


def list_escalations(*, user_ids=None, statuses=("open", "acknowledged"), level: str | None = None,
                     limit: int = 100_000) -> list[dict]:
    c = conn()
    try:
        return _rows(c.execute(
            """
            SELECT e.*, u.full_name AS employee_name, u.manager_id, m.full_name AS manager_name
            FROM performance_escalations e JOIN users u ON u.user_id = e.user_id LEFT JOIN users m ON m.user_id = u.manager_id
            WHERE e.status IN (SELECT value FROM json_each(?))
              AND (? IS NULL OR e.user_id IN (SELECT value FROM json_each(?))) AND (? IS NULL OR e.level = ?)
            ORDER BY CASE e.level WHEN 'leader' THEN 0 ELSE 1 END, e.created_at DESC LIMIT ?
            """, (json.dumps(list(statuses)), None if user_ids is None else 1, _ids_param(user_ids or []), level, level,
                  limit)).fetchall())
    finally:
        c.close()


def escalation_counts(user_ids=None) -> dict[str, int]:
    """Open or acknowledged escalations per level (all, or within a scope)."""
    c = conn()
    try:
        counts = {"manager": 0, "leader": 0}
        for r in c.execute("SELECT level, COUNT(*) FROM performance_escalations WHERE status != 'resolved' "
                           "AND (? IS NULL OR user_id IN (SELECT value FROM json_each(?))) GROUP BY level",
                           (None if user_ids is None else 1, _ids_param(user_ids or []))).fetchall():
            counts[r[0]] = r[1]
        return counts
    finally:
        c.close()


def get_escalation(escalation_id: str) -> dict | None:
    c = conn()
    try:
        row = c.execute("SELECT * FROM performance_escalations WHERE escalation_id = ?", (escalation_id,)).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def set_escalation_status(escalation_id: str, status: str, by: str, note: str) -> bool:
    c = conn()
    try:
        cur = c.execute("UPDATE performance_escalations SET status = ?, handled_by = ?, note = ?, updated_at = ? "
                        "WHERE escalation_id = ? AND status != 'resolved'", (status, by, note, _now(), escalation_id))
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def resolve_open_escalations(user_id: str, note: str) -> int:
    c = conn()
    try:
        cur = c.execute("UPDATE performance_escalations SET status = 'resolved', handled_by = 'system', note = ?, updated_at = ? "
                        "WHERE user_id = ? AND status != 'resolved'", (note, _now(), user_id))
        c.commit()
        return cur.rowcount
    finally:
        c.close()


def save_insight(user_id: str, insight: dict, generated_by: str, requested_by: str) -> str:
    c = conn()
    try:
        insight_id = _id("INS")
        c.execute("INSERT INTO performance_insights (insight_id, user_id, insight_json, generated_by, requested_by, created_at) "
                  "VALUES (?, ?, ?, ?, ?, ?)", (insight_id, user_id, json.dumps(insight), generated_by, requested_by, _now()))
        c.commit()
        return insight_id
    finally:
        c.close()


def latest_insight(user_id: str) -> dict | None:
    c = conn()
    try:
        row = c.execute("SELECT * FROM performance_insights WHERE user_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                        (user_id,)).fetchone()
        if not row:
            return None
        return {**dict(row), "insight": json.loads(row["insight_json"])}
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Status snapshot (read model)
# ---------------------------------------------------------------------------

_STATUS_COLUMNS = ("user_id", "full_name", "manager_id", "dept_id", "department", "role_id", "role_name", "tower", "band",
                   "score", "readiness_pct", "promotion_level", "signals_json", "factors_json", "band_since", "computed_at",
                   "attention_rank")


def drop_status(user_id: str) -> None:
    """Someone who is no longer an employee leaves the people views."""
    c = conn()
    try:
        c.execute("DELETE FROM employee_status WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM employee_signals WHERE user_id = ?", (user_id,))
        c.commit()
    finally:
        c.close()


def upsert_status(rows: list[dict]) -> None:
    if not rows:
        return
    marks = ", ".join("?" * len(_STATUS_COLUMNS))
    updates = ", ".join(f"{col} = excluded.{col}" for col in _STATUS_COLUMNS if col != "user_id")
    c = conn()
    try:
        # Fixed column list (bandit B608 reviewed).
        c.executemany(
            f"INSERT INTO employee_status ({', '.join(_STATUS_COLUMNS)}) VALUES ({marks}) "  # nosec B608
            f"ON CONFLICT(user_id) DO UPDATE SET {updates}",
            [tuple(r.get(col) for col in _STATUS_COLUMNS) for r in rows])
        c.execute("DELETE FROM employee_signals WHERE user_id IN (SELECT value FROM json_each(?))",
                  (_ids_param(r["user_id"] for r in rows),))
        c.executemany("INSERT OR IGNORE INTO employee_signals (user_id, signal) VALUES (?, ?)",
                      [(r["user_id"], s["key"]) for r in rows for s in json.loads(r.get("signals_json") or "[]")])
        c.commit()
    finally:
        c.close()


def status_rows(user_ids) -> dict[str, dict]:
    c = conn()
    try:
        return {r["user_id"]: dict(r) for r in c.execute(
            "SELECT * FROM employee_status WHERE user_id IN (SELECT value FROM json_each(?))", (_ids_param(user_ids),)).fetchall()}
    finally:
        c.close()


def _status_where(filters: dict) -> tuple[str, list]:
    clauses, params = ["1 = 1"], []
    if filters.get("manager_id"):
        clauses.append("s.manager_id = ?")
        params.append(filters["manager_id"])
    if filters.get("no_manager"):
        clauses.append("s.manager_id IS NULL")
    if filters.get("dept_ids") is not None:
        clauses.append("s.dept_id IN (SELECT value FROM json_each(?))")
        params.append(json.dumps(filters["dept_ids"]))
    if filters.get("dept_id"):
        clauses.append("s.dept_id = ?")
        params.append(filters["dept_id"])
    if filters.get("band"):
        clauses.append("s.band = ?")
        params.append(filters["band"])
    if filters.get("tower"):
        clauses.append("s.tower = ?")
        params.append(filters["tower"])
    if filters.get("role_id"):
        clauses.append("s.role_id = ?")
        params.append(filters["role_id"])
    if filters.get("signal"):
        # Signals are stored as a JSON array of {"key": ...}; match on the key.
        clauses.append("s.user_id IN (SELECT user_id FROM employee_signals WHERE signal = ?)")
        params.append(filters["signal"])
    if filters.get("q"):
        clauses.append("(s.full_name LIKE ? ESCAPE '\\' OR s.role_name LIKE ? ESCAPE '\\' OR s.user_id LIKE ? ESCAPE '\\')")
        term = "%" + filters["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params += [term, term, term]
    return " AND ".join(clauses), params


_SORTS = {"attention": "s.attention_rank, s.full_name", "name": "s.full_name", "score_desc": "s.score DESC, s.full_name",
          "score_asc": "s.score, s.full_name"}


def query_status(filters: dict, *, page: int = 1, page_size: int = 24, sort: str = "attention") -> tuple[list[dict], int]:
    where, params = _status_where(filters)
    order = _SORTS.get(sort, _SORTS["attention"])
    c = conn()
    try:
        # WHERE is built from fixed fragments; every value is bound (bandit B608 reviewed).
        total = c.execute(f"SELECT COUNT(*) FROM employee_status s WHERE {where}", params).fetchone()[0]  # nosec B608
        rows = c.execute(f"SELECT s.* FROM employee_status s WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",  # nosec B608
                         (*params, page_size, (max(page, 1) - 1) * page_size)).fetchall()
        return _rows(rows), total
    finally:
        c.close()


def status_counts(filters: dict, group: str = "band") -> list[dict]:
    column = {"band": "s.band", "department": "COALESCE(s.department, '—')", "tower": "COALESCE(s.tower, '—')",
              "promotion": "COALESCE(s.promotion_level, '—')"}[group]
    where, params = _status_where(filters)
    c = conn()
    try:
        # Column from the fixed map above, WHERE from fixed fragments; values bound (bandit B608 reviewed).
        sql = (f"SELECT {column} AS name, s.band AS band, COUNT(*) AS n, ROUND(AVG(s.score)) AS avg_score "  # nosec B608
               f"FROM employee_status s WHERE {where} GROUP BY 1, 2")
        return _rows(c.execute(sql, params).fetchall())
    finally:
        c.close()


def band_counts(filters: dict) -> dict[str, int]:
    """Employees per band within the filters (an index-only GROUP BY)."""
    where, params = _status_where(filters)
    c = conn()
    try:
        return {r[0]: r[1] for r in c.execute(  # nosec B608 -- fixed fragments, bound values
            f"SELECT s.band, COUNT(*) FROM employee_status s WHERE {where} GROUP BY s.band", params).fetchall()}
    finally:
        c.close()


_CLUSTER_SORTS = {
    # Share of the team at risk or off track first: where a leader's attention is needed.
    "attention": "(c.off_track + c.at_risk) * 1.0 / c.team DESC, c.off_track DESC, c.manager_name",
    "name": "c.manager_name", "team_desc": "c.team DESC, c.manager_name", "score_asc": "c.avg_score IS NULL, c.avg_score, c.manager_name",
    "score_desc": "c.avg_score IS NULL, c.avg_score DESC, c.manager_name",
}


def manager_clusters(filters: dict, *, page: int = 1, page_size: int = 24, sort: str = "attention") -> tuple[list[dict], int]:
    """Employees in scope grouped by line manager, with each team's band mix and signals.
    One GROUP BY over the snapshot: 20k employees / 2k managers stays a single indexed query."""
    where, params = _status_where(filters)
    order = _CLUSTER_SORTS.get(sort, _CLUSTER_SORTS["attention"])
    c = conn()
    try:
        # WHERE from fixed fragments, ORDER BY from the fixed map above; values bound (bandit B608 reviewed).
        base = f"""
            SELECT s.manager_id, COALESCE(m.full_name, 'No manager assigned') AS manager_name, m.job_title AS manager_title,
                   COUNT(*) AS team, ROUND(AVG(s.score)) AS avg_score,
                   SUM(s.band = 'Exceeding') AS exceeding, SUM(s.band = 'On track') AS on_track,
                   SUM(s.band = 'At risk') AS at_risk, SUM(s.band = 'Off track') AS off_track,
                   SUM(s.band = 'Not enough data') AS no_data,
                   SUM(COALESCE(g.promotion_ready, 0)) AS promotion_ready, SUM(COALESCE(g.escalation, 0)) AS escalated,
                   SUM(COALESCE(g.concern, 0)) AS concerns, SUM(COALESCE(g.probation, 0)) AS probation,
                   COUNT(DISTINCT s.dept_id) AS departments, GROUP_CONCAT(DISTINCT s.tower) AS towers
            FROM employee_status s
            LEFT JOIN users m ON m.user_id = s.manager_id
            LEFT JOIN (SELECT user_id, MAX(signal = 'promotion_ready') AS promotion_ready, MAX(signal = 'escalation') AS escalation,
                              MAX(signal = 'performance_concern') AS concern,
                              MAX(signal IN ('probation', 'probation_decision')) AS probation
                       FROM employee_signals GROUP BY user_id) g ON g.user_id = s.user_id
            WHERE {where} GROUP BY s.manager_id"""  # nosec B608
        total = c.execute(f"SELECT COUNT(*) FROM ({base})", params).fetchone()[0]  # nosec B608
        rows = c.execute(f"SELECT * FROM ({base}) c ORDER BY {order} LIMIT ? OFFSET ?",  # nosec B608
                         (*params, page_size, (max(page, 1) - 1) * page_size)).fetchall()
        return _rows(rows), total
    finally:
        c.close()


def missing_status_count() -> int:
    """Employees without a snapshot row: two index counts, not a join over everyone."""
    c = conn()
    try:
        return c.execute("SELECT (SELECT COUNT(*) FROM users WHERE role = 'employee') - "
                         "(SELECT COUNT(*) FROM employee_status)").fetchone()[0]
    finally:
        c.close()


def probation_counts(user_ids, today: str, soon: str) -> dict:
    """Tracker headline numbers in SQL (the leader overview doesn't need every row)."""
    scoped = "AND p.user_id IN (SELECT value FROM json_each(?))" if user_ids is not None else ""
    params = (_ids_param(user_ids),) if user_ids is not None else ()
    c = conn()
    try:
        row = c.execute(  # nosec B608 -- fixed fragment, bound values
            f"""
            SELECT SUM(p.status IN ('active', 'pending_leader')), SUM(p.status = 'pending_leader'),
                   SUM(p.status = 'active' AND p.end_date <= ?),
                   (SELECT COUNT(*) FROM probation_checkpoints k JOIN probation_records r ON r.probation_id = k.probation_id
                    WHERE r.status = 'active' AND k.completed_at IS NULL AND k.due_date < ?
                    {scoped.replace('p.', 'r.')})
            FROM probation_records p WHERE 1 = 1 {scoped}
            """, (soon, today, *params, *params)).fetchone()
        return {"open": row[0] or 0, "pending_leader": row[1] or 0, "decision_due": row[2] or 0, "overdue_checkins": row[3] or 0}
    finally:
        c.close()


def status_ids(filters: dict) -> set[str]:
    where, params = _status_where(filters)
    c = conn()
    try:
        return {r[0] for r in c.execute(f"SELECT s.user_id FROM employee_status s WHERE {where}", params).fetchall()}  # nosec B608
    finally:
        c.close()


def stale_status_ids(before: str, limit: int = 5000) -> list[str]:
    """Employees with no snapshot or one computed before `before` (ISO timestamp)."""
    c = conn()
    try:
        return [r[0] for r in c.execute(
            "SELECT u.user_id FROM users u LEFT JOIN employee_status s ON s.user_id = u.user_id "
            "WHERE u.role = 'employee' AND (s.computed_at IS NULL OR s.computed_at < ?) ORDER BY s.computed_at LIMIT ?",
            (before, limit)).fetchall()]
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Job profiles, skill requirement changes, trends
# ---------------------------------------------------------------------------

def job_profiles() -> dict[str, dict]:
    c = conn()
    try:
        return {r["role_id"]: dict(r) for r in c.execute(
            "SELECT j.*, d.name AS department_name FROM job_profiles j LEFT JOIN departments d ON d.dept_id = j.dept_id").fetchall()}
    finally:
        c.close()


def upsert_job_profile(role_id: str, *, dept_id: str | None, description: str | None, by: str) -> None:
    c = conn()
    try:
        c.execute(
            """
            INSERT INTO job_profiles (role_id, dept_id, description, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(role_id) DO UPDATE SET dept_id = COALESCE(excluded.dept_id, dept_id),
                description = COALESCE(excluded.description, description), updated_at = excluded.updated_at
            """, (role_id, dept_id, description, by, _now(), _now()))
        c.commit()
    finally:
        c.close()


def create_role(*, role_id: str, tower: str, process_group: str, role_name: str, role_grade: str,
                summary: str, responsibilities: str, skills_text: str) -> bool:
    """Adds the role to the reference catalogue (role_master + role_descriptions for resume matching)."""
    c = conn()
    try:
        c.execute("INSERT INTO role_master (role_id, tower, process_group, role_name, role_grade, level_group) "
                  "VALUES (?, ?, ?, ?, ?, ?)", (role_id, tower, process_group, role_name, role_grade, role_grade))
        c.execute("INSERT OR REPLACE INTO role_descriptions (role_id, role_summary, responsibility_extract, output_extract, "
                  "skills_extract, source_type, selection_status) VALUES (?, ?, ?, '', ?, 'talent360i', 'active')",
                  (role_id, summary, responsibilities, skills_text))
        c.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        c.close()


def role_skill_rows(role_id: str) -> list[dict]:
    c = conn()
    try:
        return _rows(c.execute(
            """
            SELECT rsm.role_skill_id, rsm.skill_id, rsm.skill, rsm.capability, rsm.target_proficiency_level AS target_level,
                   rsm.is_critical, rsm.behavior_indicator, rsm.is_synthetic_grounding
            FROM role_skill_map rsm WHERE rsm.role_id = ?
            ORDER BY rsm.is_critical DESC, rsm.target_proficiency_level DESC, rsm.skill
            """, (role_id,)).fetchall())
    finally:
        c.close()


def skill_master_rows() -> list[dict]:
    c = conn()
    try:
        return _rows(c.execute("SELECT skill_id, tower, skill, capability FROM skill_master ORDER BY tower, skill").fetchall())
    finally:
        c.close()


def apply_skill_change(role_id: str, action: str, skill: str, payload: dict) -> bool:
    """Writes one requirement change to role_skill_map (and skill_master for a brand-new skill)."""
    c = conn()
    try:
        existing = c.execute("SELECT role_skill_id FROM role_skill_map WHERE role_id = ? AND lower(skill) = lower(?)",
                             (role_id, skill)).fetchone()
        if action == "remove":
            if not existing:
                return False
            c.execute("DELETE FROM role_skill_map WHERE role_skill_id = ?", (existing["role_skill_id"],))
        elif action == "update":
            if not existing:
                return False
            c.execute("UPDATE role_skill_map SET target_proficiency_level = ?, is_critical = ?, "
                      "behavior_indicator = COALESCE(?, behavior_indicator) WHERE role_skill_id = ?",
                      (payload["target_level"], 1 if payload.get("is_critical") else 0, payload.get("behavior_indicator") or None,
                       existing["role_skill_id"]))
        else:
            if existing:
                return False
            role = c.execute("SELECT tower FROM role_master WHERE role_id = ?", (role_id,)).fetchone()
            master = c.execute("SELECT skill_id, capability FROM skill_master WHERE lower(skill) = lower(?)", (skill,)).fetchone()
            if master:
                skill_id, capability = master["skill_id"], payload.get("capability") or master["capability"]
            else:
                skill_id, capability = _id("SK"), payload.get("capability") or "Emerging"
                generic = dict(c.execute("SELECT level, behavior_indicator FROM proficiency_levels").fetchall())
                c.execute("INSERT INTO skill_master (skill_id, tower, skill, capability, l1_indicator, l2_indicator, l3_indicator, "
                          "l4_indicator, l5_indicator) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (skill_id, role["tower"] if role else None, skill, capability,
                           *(generic.get(n) for n in range(1, 6))))
            c.execute("INSERT INTO role_skill_map (role_skill_id, role_id, skill_id, skill, capability, target_proficiency_level, "
                      "target_band, behavior_indicator, is_critical, assess_method, is_synthetic_grounding) "
                      "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, 'question', 0)",
                      (_id("RSM"), role_id, skill_id, skill, capability, payload["target_level"],
                       payload.get("behavior_indicator"), 1 if payload.get("is_critical") else 0))
        c.commit()
        return True
    finally:
        c.close()


def add_skill_change(*, role_id: str, action: str, skill: str, payload: dict, reason: str, trend_id: str | None,
                     proposed_by: str) -> str:
    c = conn()
    try:
        change_id = _id("CHG")
        c.execute("INSERT INTO skill_requirement_changes (change_id, role_id, action, skill, payload_json, reason, trend_id, "
                  "status, proposed_by, proposed_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?)",
                  (change_id, role_id, action, skill, json.dumps(payload), reason, trend_id or None, proposed_by, _now()))
        c.commit()
        return change_id
    finally:
        c.close()


def get_skill_change(change_id: str) -> dict | None:
    c = conn()
    try:
        row = c.execute("SELECT * FROM skill_requirement_changes WHERE change_id = ?", (change_id,)).fetchone()
        return {**dict(row), "payload": json.loads(row["payload_json"] or "{}")} if row else None
    finally:
        c.close()


def mark_skill_change(change_id: str, status: str, by: str, note: str) -> bool:
    c = conn()
    try:
        cur = c.execute("UPDATE skill_requirement_changes SET status = ?, decided_by = ?, decided_at = ?, decision_note = ? "
                        "WHERE change_id = ? AND status = 'proposed'", (status, by, _now(), note, change_id))
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def list_skill_changes(*, role_id: str | None = None, status: str | None = None, limit: int = 200) -> list[dict]:
    c = conn()
    try:
        rows = c.execute(
            """
            SELECT ch.*, p.full_name AS proposed_by_name, p.role AS proposed_by_role, d.full_name AS decided_by_name,
                   rm.role_name
            FROM skill_requirement_changes ch
            LEFT JOIN users p ON p.user_id = ch.proposed_by LEFT JOIN users d ON d.user_id = ch.decided_by
            LEFT JOIN role_master rm ON rm.role_id = ch.role_id
            WHERE (? IS NULL OR ch.role_id = ?) AND (? IS NULL OR ch.status = ?)
            ORDER BY ch.proposed_at DESC LIMIT ?
            """, (role_id, role_id, status, status, limit)).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload_json"] or "{}")} for r in rows]
    finally:
        c.close()


def add_trend(*, skill: str, tower: str | None, direction: str, note: str, source: str, added_by: str) -> str:
    c = conn()
    try:
        trend_id = _id("TRD")
        c.execute("INSERT INTO skill_trends (trend_id, skill, tower, direction, note, source, added_by, created_at) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (trend_id, skill, tower or None, direction, note, source, added_by, _now()))
        c.commit()
        return trend_id
    finally:
        c.close()


def list_trends(tower: str | None = None) -> list[dict]:
    c = conn()
    try:
        return _rows(c.execute(
            "SELECT t.*, u.full_name AS added_by_name FROM skill_trends t LEFT JOIN users u ON u.user_id = t.added_by "
            "WHERE (? IS NULL OR t.tower IS NULL OR t.tower = ?) "
            "ORDER BY CASE t.direction WHEN 'emerging' THEN 0 WHEN 'rising' THEN 1 WHEN 'stable' THEN 2 ELSE 3 END, t.created_at DESC",
            (tower, tower)).fetchall())
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Verticals, the skill catalogue and Agent 8 suggestions
# ---------------------------------------------------------------------------

def list_verticals() -> list[dict]:
    c = conn()
    try:
        return _rows(c.execute(
            """
            SELECT v.*, u.full_name AS created_by_name,
                   (SELECT COUNT(*) FROM role_master r WHERE r.tower = v.code) AS roles,
                   (SELECT COUNT(*) FROM departments d WHERE d.tower = v.code) AS departments,
                   (SELECT COUNT(*) FROM skill_master k WHERE k.tower = v.code) AS skills,
                   (SELECT COUNT(*) FROM employee_status s WHERE s.tower = v.code) AS people
            FROM verticals v LEFT JOIN users u ON u.user_id = v.created_by
            ORDER BY v.created_by = 'seed' DESC, v.name
            """).fetchall())
    finally:
        c.close()


def vertical_codes() -> list[str]:
    c = conn()
    try:
        return [r[0] for r in c.execute("SELECT code FROM verticals ORDER BY created_by = 'seed' DESC, name").fetchall()]
    finally:
        c.close()


def create_vertical(*, code: str, name: str, description: str, created_by: str) -> bool:
    """False when the code or the name is already taken."""
    c = conn()
    try:
        c.execute("INSERT INTO verticals (code, name, description, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                  (code, name, description, created_by, _now()))
        c.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        c.close()


def skill_catalogue(tower: str | None = None) -> list[dict]:
    """Every catalogue skill with how many job profiles require it (and how many as critical)."""
    c = conn()
    try:
        return _rows(c.execute(
            """
            SELECT k.*, COUNT(r.role_skill_id) AS roles, COALESCE(SUM(r.is_critical), 0) AS critical_in
            FROM skill_master k LEFT JOIN role_skill_map r ON lower(r.skill) = lower(k.skill)
            WHERE (? IS NULL OR k.tower = ?)
            GROUP BY k.skill_id ORDER BY k.tower, k.skill
            """, (tower, tower)).fetchall())
    finally:
        c.close()


def skill_by_name(name: str) -> dict | None:
    c = conn()
    try:
        row = c.execute("SELECT * FROM skill_master WHERE lower(skill) = lower(?)", (name,)).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def generic_indicators() -> dict[int, str]:
    c = conn()
    try:
        return {r[0]: r[1] for r in c.execute("SELECT level, behavior_indicator FROM proficiency_levels").fetchall()}
    finally:
        c.close()


def create_skill(*, tower: str | None, skill: str, capability: str, indicators: list[str]) -> str | None:
    """Adds a skill to the catalogue (skill_master). None when the name already exists."""
    c = conn()
    try:
        if c.execute("SELECT 1 FROM skill_master WHERE lower(skill) = lower(?)", (skill,)).fetchone():
            return None
        skill_id = _id("SK")
        c.execute("INSERT INTO skill_master (skill_id, tower, skill, capability, l1_indicator, l2_indicator, l3_indicator, "
                  "l4_indicator, l5_indicator) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (skill_id, tower or None, skill, capability, *indicators[:5]))
        c.commit()
        return skill_id
    finally:
        c.close()


def add_skill_suggestions(*, batch_id: str, scope: str, role_id: str | None, tower: str | None, items: list[dict],
                          generated_by: str, created_by: str) -> list[str]:
    """Stores a fresh batch and retires the previous pending batch for the same target."""
    c = conn()
    try:
        c.execute("UPDATE skill_suggestions SET status = 'dismissed', decided_by = ?, decided_at = ? "
                  "WHERE status = 'pending' AND scope = ? AND COALESCE(role_id, '') = COALESCE(?, '') "
                  "AND (? = 'role' OR COALESCE(tower, '') = COALESCE(?, ''))",
                  (created_by, _now(), scope, role_id, scope, tower))
        ids = []
        for item in items:
            sid = _id("SUG")
            c.execute("INSERT INTO skill_suggestions (suggestion_id, batch_id, scope, role_id, tower, skill, existing, payload_json, "
                      "rationale, generated_by, status, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                      (sid, batch_id, scope, role_id, tower, item["skill"], 1 if item.get("existing") else 0, json.dumps(item),
                       item.get("rationale") or "", generated_by, created_by, _now()))
            ids.append(sid)
        c.commit()
        return ids
    finally:
        c.close()


def _parse_suggestion(row) -> dict:
    item = dict(row)
    item["payload"] = json.loads(item.pop("payload_json") or "{}")
    return item


def list_skill_suggestions(*, scope: str, role_id: str | None = None, tower: str | None = None,
                           statuses: tuple[str, ...] = ("pending",), limit: int = 60) -> list[dict]:
    c = conn()
    try:
        rows = c.execute(
            """
            SELECT g.*, u.full_name AS created_by_name, rm.role_name
            FROM skill_suggestions g LEFT JOIN users u ON u.user_id = g.created_by
            LEFT JOIN role_master rm ON rm.role_id = g.role_id
            WHERE g.scope = ? AND (? IS NULL OR g.role_id = ?) AND (? IS NULL OR g.tower = ?)
              AND g.status IN (SELECT value FROM json_each(?))
            ORDER BY g.created_at DESC, g.rowid LIMIT ?
            """, (scope, role_id, role_id, tower, tower, json.dumps(list(statuses)), limit)).fetchall()
        return [_parse_suggestion(r) for r in rows]
    finally:
        c.close()


def get_skill_suggestion(suggestion_id: str) -> dict | None:
    c = conn()
    try:
        row = c.execute("SELECT * FROM skill_suggestions WHERE suggestion_id = ?", (suggestion_id,)).fetchone()
        return _parse_suggestion(row) if row else None
    finally:
        c.close()


def mark_skill_suggestion(suggestion_id: str, status: str, by: str) -> bool:
    c = conn()
    try:
        cur = c.execute("UPDATE skill_suggestions SET status = ?, decided_by = ?, decided_at = ? "
                        "WHERE suggestion_id = ? AND status = 'pending'", (status, by, _now(), suggestion_id))
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def pending_suggestion_counts() -> dict[str, int]:
    c = conn()
    try:
        return {r[0]: r[1] for r in c.execute(
            "SELECT COALESCE(role_id, 'catalogue'), COUNT(*) FROM skill_suggestions WHERE status = 'pending' GROUP BY 1").fetchall()}
    finally:
        c.close()


def role_description(role_id: str) -> dict:
    c = conn()
    try:
        row = c.execute("SELECT * FROM role_descriptions WHERE role_id = ?", (role_id,)).fetchone()
        return dict(row) if row else {}
    finally:
        c.close()


def role_readiness_counts(role_id: str) -> dict:
    """What a job profile needs before people can be assessed against it: approved bank
    questions, and catalogue courses that train each required skill."""
    c = conn()
    try:
        try:
            approved = c.execute("SELECT COUNT(*) FROM bank_questions WHERE role_id = ? AND sme_status = 'approved'",
                                 (role_id,)).fetchone()[0]
        except sqlite3.OperationalError:
            approved = 0  # the bank table appears with the first SME generation
        trained = {r[0] for r in c.execute(
            "SELECT DISTINCT lower(r.skill) FROM role_skill_map r JOIN training_skill_map t "
            "ON t.skill_id = r.skill_id OR lower(t.skill) = lower(r.skill) WHERE r.role_id = ?", (role_id,)).fetchall()}
        return {"approved_questions": approved, "trained_skills": trained}
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Performance summaries (Agent 9)
# ---------------------------------------------------------------------------

def save_summary(*, user_id: str, review_type: str, target_role_id: str | None, window: tuple[str, str], facts: dict,
                 summary: dict, generated_by: str, requested_by: str) -> str:
    c = conn()
    try:
        summary_id = _id("SUM")
        c.execute("INSERT INTO performance_summaries (summary_id, user_id, review_type, target_role_id, window_from, window_to, "
                  "facts_json, summary_json, generated_by, requested_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (summary_id, user_id, review_type, target_role_id, window[0], window[1], json.dumps(facts), json.dumps(summary),
                   generated_by, requested_by, _now()))
        c.commit()
        return summary_id
    finally:
        c.close()


def _parse_summary(row) -> dict:
    item = dict(row)
    item["facts"] = json.loads(item.pop("facts_json") or "{}")
    item["summary"] = json.loads(item.pop("summary_json") or "{}")
    return item


def latest_summaries(user_id: str) -> dict[str, dict]:
    """The newest summary of each review type for one employee."""
    c = conn()
    try:
        rows = c.execute(
            "SELECT s.*, u.full_name AS requested_by_name FROM performance_summaries s LEFT JOIN users u ON u.user_id = s.requested_by "
            "WHERE s.user_id = ? ORDER BY s.created_at DESC, s.rowid DESC", (user_id,)).fetchall()
        out: dict[str, dict] = {}
        for row in rows:
            out.setdefault(row["review_type"], _parse_summary(row))
        return out
    finally:
        c.close()


def get_summary(summary_id: str) -> dict | None:
    c = conn()
    try:
        row = c.execute("SELECT * FROM performance_summaries WHERE summary_id = ?", (summary_id,)).fetchone()
        return _parse_summary(row) if row else None
    finally:
        c.close()


def completed_training(user_id: str, since: str, until: str) -> list[dict]:
    c = conn()
    try:
        return _rows(c.execute(
            "SELECT title, skill, event_date, course_id FROM scheduled_events WHERE user_id = ? AND event_type = 'training' "
            "AND status = 'completed' AND event_date BETWEEN ? AND ? ORDER BY event_date DESC", (user_id, since, until)).fetchall())
    finally:
        c.close()


def upcoming_training_count(user_id: str, today: str) -> int:
    c = conn()
    try:
        return c.execute("SELECT COUNT(*) FROM scheduled_events WHERE user_id = ? AND event_type = 'training' "
                         "AND status = 'scheduled' AND event_date >= ?", (user_id, today)).fetchone()[0]
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Career mobility: aspirations, CV claims, internal postings, applications, shortlists
# ---------------------------------------------------------------------------

def list_aspirations(*, user_id: str | None = None, role_ids=None, statuses=("active",)) -> list[dict]:
    c = conn()
    try:
        return _rows(c.execute(
            """
            SELECT a.*, u.full_name, u.job_title, u.manager_id, rm.role_name, rm.tower, rm.role_grade,
                   p.title AS posting_title, p.apply_url
            FROM career_aspirations a JOIN users u ON u.user_id = a.user_id
            LEFT JOIN role_master rm ON rm.role_id = a.role_id LEFT JOIN job_postings p ON p.posting_id = a.posting_id
            WHERE a.status IN (SELECT value FROM json_each(?)) AND (? IS NULL OR a.user_id = ?)
              AND (? IS NULL OR a.role_id IN (SELECT value FROM json_each(?)))
            ORDER BY a.created_at, a.rowid
            """, (json.dumps(list(statuses)), user_id, user_id, None if role_ids is None else 1,
                  json.dumps(list(role_ids or [])))).fetchall())
    finally:
        c.close()


def add_aspiration(*, user_id: str, role_id: str | None, posting_id: str | None, note: str) -> str:
    c = conn()
    try:
        aspiration_id = _id("ASP")
        c.execute("INSERT INTO career_aspirations (aspiration_id, user_id, role_id, posting_id, note, status, created_at, updated_at) "
                  "VALUES (?, ?, ?, ?, ?, 'active', ?, ?)", (aspiration_id, user_id, role_id, posting_id, note, _now(), _now()))
        c.commit()
        return aspiration_id
    finally:
        c.close()


def withdraw_aspiration(aspiration_id: str, user_id: str) -> bool:
    c = conn()
    try:
        cur = c.execute("UPDATE career_aspirations SET status = 'withdrawn', updated_at = ? WHERE aspiration_id = ? AND user_id = ? "
                        "AND status = 'active'", (_now(), aspiration_id, user_id))
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def replace_cv_claims(user_id: str, claims: list[dict], generated_by: str, source_file: str) -> int:
    c = conn()
    try:
        c.execute("DELETE FROM cv_skill_claims WHERE user_id = ?", (user_id,))
        for claim in claims:
            c.execute("INSERT OR REPLACE INTO cv_skill_claims (claim_id, user_id, skill, level, quote, generated_by, source_file, created_at) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (_id("CVC"), user_id, claim["skill"], claim["level"], claim.get("quote"),
                                                          generated_by, source_file, _now()))
        c.commit()
        return len(claims)
    finally:
        c.close()


def cv_claims(user_ids) -> dict[str, list[dict]]:
    c = conn()
    try:
        out: dict[str, list[dict]] = {}
        for r in c.execute("SELECT * FROM cv_skill_claims WHERE user_id IN (SELECT value FROM json_each(?)) ORDER BY skill",
                           (_ids_param(user_ids),)).fetchall():
            out.setdefault(r["user_id"], []).append(dict(r))
        return out
    finally:
        c.close()


def clear_cv_claims(user_id: str) -> int:
    c = conn()
    try:
        cur = c.execute("DELETE FROM cv_skill_claims WHERE user_id = ?", (user_id,))
        c.commit()
        return cur.rowcount
    finally:
        c.close()


def upsert_posting(posting: dict) -> None:
    """Insert or refresh a posting; an HR mapping to a job profile survives the refresh."""
    c = conn()
    try:
        c.execute(
            """
            INSERT INTO job_postings (posting_id, source, title, department, location, experience_level, employment_type,
                released_on, apply_url, description, qualifications, role_id, mapped_by, hiring_manager_id, status, fetched_at)
            VALUES (:posting_id, :source, :title, :department, :location, :experience_level, :employment_type, :released_on,
                    :apply_url, :description, :qualifications, :role_id, :mapped_by, :hiring_manager_id, 'open', :fetched_at)
            ON CONFLICT(posting_id) DO UPDATE SET title = excluded.title, department = excluded.department,
                location = excluded.location, experience_level = excluded.experience_level,
                employment_type = excluded.employment_type, released_on = excluded.released_on, apply_url = excluded.apply_url,
                description = excluded.description, qualifications = excluded.qualifications, status = 'open',
                fetched_at = excluded.fetched_at,
                role_id = CASE WHEN job_postings.mapped_by IS NOT NULL AND job_postings.mapped_by != 'auto'
                               THEN job_postings.role_id ELSE excluded.role_id END,
                mapped_by = CASE WHEN job_postings.mapped_by IS NOT NULL AND job_postings.mapped_by != 'auto'
                                 THEN job_postings.mapped_by ELSE excluded.mapped_by END
            """, {**{k: None for k in ("department", "location", "experience_level", "employment_type", "released_on",
                                        "apply_url", "description", "qualifications", "role_id", "mapped_by",
                                        "hiring_manager_id")}, **posting, "fetched_at": _now()})
        c.commit()
    finally:
        c.close()


def close_postings_except(source: str, open_ids: list[str]) -> int:
    c = conn()
    try:
        cur = c.execute("UPDATE job_postings SET status = 'closed' WHERE source = ? AND status = 'open' "
                        "AND posting_id NOT IN (SELECT value FROM json_each(?))", (source, json.dumps(open_ids)))
        c.commit()
        return cur.rowcount
    finally:
        c.close()


def list_postings(*, status: str | None = "open", role_id: str | None = None) -> list[dict]:
    c = conn()
    try:
        return _rows(c.execute(
            """
            SELECT p.*, rm.role_name, rm.tower, rm.role_grade FROM job_postings p LEFT JOIN role_master rm ON rm.role_id = p.role_id
            WHERE (? IS NULL OR p.status = ?) AND (? IS NULL OR p.role_id = ?)
            ORDER BY p.released_on DESC, p.title
            """, (status, status, role_id, role_id)).fetchall())
    finally:
        c.close()


def get_posting(posting_id: str) -> dict | None:
    c = conn()
    try:
        row = c.execute("SELECT p.*, rm.role_name, rm.tower FROM job_postings p LEFT JOIN role_master rm ON rm.role_id = p.role_id "
                        "WHERE p.posting_id = ?", (posting_id,)).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def map_posting(posting_id: str, role_id: str | None, by: str) -> bool:
    c = conn()
    try:
        cur = c.execute("UPDATE job_postings SET role_id = ?, mapped_by = ? WHERE posting_id = ?", (role_id, by, posting_id))
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def posting_sync_state() -> dict:
    c = conn()
    try:
        row = c.execute("SELECT source, MAX(fetched_at), SUM(status = 'open') FROM job_postings GROUP BY source "
                        "ORDER BY MAX(fetched_at) DESC LIMIT 1").fetchone()
        return {"source": row[0], "fetched_at": row[1], "open": row[2] or 0} if row else {"source": None, "fetched_at": None, "open": 0}
    finally:
        c.close()


def add_application(*, user_id: str, role_id: str | None, posting_id: str | None, note: str, readiness: dict) -> str | None:
    """None when this person already has an open application for the same role or posting."""
    c = conn()
    try:
        if c.execute("SELECT 1 FROM internal_applications WHERE user_id = ? AND status IN ('submitted', 'shortlisted') "
                     "AND (COALESCE(role_id, '') = COALESCE(?, '') AND COALESCE(posting_id, '') = COALESCE(?, ''))",
                     (user_id, role_id, posting_id)).fetchone():
            return None
        application_id = _id("APP")
        c.execute("INSERT INTO internal_applications (application_id, user_id, role_id, posting_id, note, readiness_json, status, "
                  "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'submitted', ?, ?)",
                  (application_id, user_id, role_id, posting_id, note, json.dumps(readiness), _now(), _now()))
        c.commit()
        return application_id
    finally:
        c.close()


def list_applications(*, user_id: str | None = None, role_id: str | None = None,
                      statuses=("submitted", "shortlisted", "declined", "withdrawn")) -> list[dict]:
    c = conn()
    try:
        rows = c.execute(
            """
            SELECT a.*, u.full_name, u.job_title, u.manager_id, rm.role_name, p.title AS posting_title, d.full_name AS decided_by_name
            FROM internal_applications a JOIN users u ON u.user_id = a.user_id
            LEFT JOIN role_master rm ON rm.role_id = a.role_id LEFT JOIN job_postings p ON p.posting_id = a.posting_id
            LEFT JOIN users d ON d.user_id = a.decided_by
            WHERE (? IS NULL OR a.user_id = ?) AND (? IS NULL OR a.role_id = ?) AND a.status IN (SELECT value FROM json_each(?))
            ORDER BY a.created_at DESC, a.rowid DESC
            """, (user_id, user_id, role_id, role_id, json.dumps(list(statuses)))).fetchall()
        return [{**dict(r), "readiness": json.loads(r["readiness_json"] or "{}")} for r in rows]
    finally:
        c.close()


def get_application(application_id: str) -> dict | None:
    c = conn()
    try:
        row = c.execute("SELECT * FROM internal_applications WHERE application_id = ?", (application_id,)).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def set_application_status(application_id: str, status: str, by: str, note: str, *, from_statuses=("submitted", "shortlisted")) -> bool:
    c = conn()
    try:
        cur = c.execute("UPDATE internal_applications SET status = ?, decided_by = ?, decision_note = ?, updated_at = ? "
                        "WHERE application_id = ? AND status IN (SELECT value FROM json_each(?))",
                        (status, by, note, _now(), application_id, json.dumps(list(from_statuses))))
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def add_shortlist(*, role_id: str, user_id: str, added_by: str, note: str) -> bool:
    c = conn()
    try:
        c.execute("INSERT INTO talent_shortlists (shortlist_id, role_id, user_id, added_by, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                  (_id("SHL"), role_id, user_id, added_by, note, _now()))
        c.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        c.close()


def shortlists(role_id: str) -> dict[str, list[dict]]:
    c = conn()
    try:
        out: dict[str, list[dict]] = {}
        for r in c.execute("SELECT s.*, u.full_name AS added_by_name FROM talent_shortlists s LEFT JOIN users u ON u.user_id = s.added_by "
                           "WHERE s.role_id = ? ORDER BY s.created_at", (role_id,)).fetchall():
            out.setdefault(r["user_id"], []).append(dict(r))
        return out
    finally:
        c.close()


def users_with_skills(skills: list[str], exclude_title: str | None, limit: int = 200) -> list[str]:
    """Employees with an assessed, certified or CV-claimed level in any of these skills, most skills first:
    the "potential" candidates for a role, found by index lookups rather than scoring everyone."""
    c = conn()
    try:
        names = json.dumps([s.lower() for s in skills])
        rows = c.execute(
            """
            SELECT x.user_id, COUNT(DISTINCT x.skill) AS n FROM (
                SELECT user_id, lower(skill) AS skill FROM skill_gaps WHERE lower(skill) IN (SELECT value FROM json_each(?))
                    AND current_level IS NOT NULL
                UNION SELECT user_id, lower(skill) FROM skill_certifications WHERE status = 'active'
                    AND lower(skill) IN (SELECT value FROM json_each(?))
                UNION SELECT user_id, lower(skill) FROM cv_skill_claims WHERE lower(skill) IN (SELECT value FROM json_each(?))
            ) x JOIN users u ON u.user_id = x.user_id
            WHERE u.role = 'employee' AND (? IS NULL OR lower(COALESCE(u.job_title, '')) != lower(?))
            GROUP BY x.user_id ORDER BY n DESC, x.user_id LIMIT ?
            """, (names, names, names, exclude_title, exclude_title, limit)).fetchall()
        return [r[0] for r in rows]
    except sqlite3.OperationalError:
        return []  # a reference table (skill_gaps / certifications) not present yet
    finally:
        c.close()


# ---------------------------------------------------------------------------
# SME coding challenges
# ---------------------------------------------------------------------------

def _parse_challenge(row) -> dict:
    item = dict(row)
    item["keywords"] = json.loads(item["keywords_json"] or "[]")
    item["skills"] = json.loads(item["skills_json"] or "[]")
    return item


def create_challenge(*, title: str, prompt: str, language: str, starter_code: str, keywords: list[str],
                     skills: list[str], difficulty: str, rubric: str, time_minutes: int, created_by: str) -> str:
    c = conn()
    try:
        challenge_id = _id("CODE")
        c.execute("INSERT INTO coding_challenges (challenge_id, title, prompt, language, starter_code, keywords_json, skills_json, "
                  "difficulty, rubric, time_minutes, status, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)",
                  (challenge_id, title, prompt, language, starter_code, json.dumps(keywords), json.dumps(skills), difficulty,
                   rubric, time_minutes, created_by, _now()))
        c.commit()
        return challenge_id
    finally:
        c.close()


def list_challenges(status: str | None = None) -> list[dict]:
    c = conn()
    try:
        return [_parse_challenge(r) for r in c.execute(
            "SELECT * FROM coding_challenges WHERE (? IS NULL OR status = ?) ORDER BY status, created_at DESC",
            (status, status)).fetchall()]
    finally:
        c.close()


def get_challenge(challenge_id: str) -> dict | None:
    c = conn()
    try:
        row = c.execute("SELECT * FROM coding_challenges WHERE challenge_id = ?", (challenge_id,)).fetchone()
        return _parse_challenge(row) if row else None
    finally:
        c.close()


def set_challenge_status(challenge_id: str, status: str, by: str) -> bool:
    c = conn()
    try:
        cur = c.execute("UPDATE coding_challenges SET status = ?, approved_by = CASE WHEN ? = 'approved' THEN ? ELSE approved_by END, "
                        "approved_at = CASE WHEN ? = 'approved' THEN ? ELSE approved_at END WHERE challenge_id = ? AND status != ?",
                        (status, status, by, status, _now(), challenge_id, status))
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def save_resume_signals(cycle_id: str, signals: dict) -> None:
    """signals: {"keywords": [...SME trigger keywords found in the resume], "challenge_ids": [...] | None}."""
    c = conn()
    try:
        c.execute("INSERT OR REPLACE INTO cycle_resume_signals (cycle_id, keywords_json, created_at) VALUES (?, ?, ?)",
                  (cycle_id, json.dumps(signals), _now()))
        c.commit()
    finally:
        c.close()


def resume_signals(cycle_id: str) -> dict:
    c = conn()
    try:
        row = c.execute("SELECT keywords_json FROM cycle_resume_signals WHERE cycle_id = ?", (cycle_id,)).fetchone()
        return json.loads(row[0]) if row else {"keywords": [], "challenge_ids": None}
    finally:
        c.close()


def save_attempt(*, cycle_id: str, challenge_id: str, user_id: str, language: str, code: str,
                 paste_chars: int | None = None) -> str | None:
    """None if this challenge was already submitted in this cycle (one attempt, like the MCQ)."""
    c = conn()
    try:
        attempt_id = _id("ATT")
        c.execute("INSERT INTO coding_attempts (attempt_id, cycle_id, challenge_id, user_id, language, code, paste_chars, "
                  "submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  (attempt_id, cycle_id, challenge_id, user_id, language, code, paste_chars, _now()))
        c.commit()
        return attempt_id
    except sqlite3.IntegrityError:
        return None
    finally:
        c.close()


def set_attempt_review(attempt_id: str, review: dict | None, status: str) -> None:
    c = conn()
    try:
        c.execute("UPDATE coding_attempts SET review_json = ?, review_status = ? WHERE attempt_id = ?",
                  (json.dumps(review) if review else None, status, attempt_id))
        c.commit()
    finally:
        c.close()


def sme_review_attempt(attempt_id: str, score: int, note: str, by: str) -> bool:
    c = conn()
    try:
        cur = c.execute("UPDATE coding_attempts SET sme_score = ?, sme_note = ?, reviewed_by = ?, reviewed_at = ?, "
                        "review_status = 'sme_reviewed' WHERE attempt_id = ?", (score, note, by, _now(), attempt_id))
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def list_attempts(*, cycle_id: str | None = None, review_status: str | None = None, limit: int = 200) -> list[dict]:
    c = conn()
    try:
        rows = c.execute(
            """
            SELECT a.*, ch.title, ch.prompt, ch.rubric, ch.language AS challenge_language, u.full_name AS employee_name,
                   r.full_name AS reviewer_name
            FROM coding_attempts a JOIN coding_challenges ch ON ch.challenge_id = a.challenge_id
            LEFT JOIN users u ON u.user_id = a.user_id LEFT JOIN users r ON r.user_id = a.reviewed_by
            WHERE (? IS NULL OR a.cycle_id = ?) AND (? IS NULL OR a.review_status = ?)
            ORDER BY a.submitted_at DESC LIMIT ?
            """, (cycle_id, cycle_id, review_status, review_status, limit)).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["review"] = json.loads(item["review_json"]) if item.get("review_json") else None
            out.append(item)
        return out
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Notifications and pulse check-ins
# ---------------------------------------------------------------------------

def add_notification(*, user_id: str, kind: str, title: str, body: str, link: str | None, channels: str) -> str:
    c = conn()
    try:
        notification_id = _id("NTF")
        c.execute("INSERT INTO notifications (notification_id, user_id, kind, title, body, link, channels, created_at) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (notification_id, user_id, kind, title, body, link, channels, _now()))
        c.commit()
        return notification_id
    finally:
        c.close()


def list_notifications(user_id: str, limit: int = 50) -> list[dict]:
    c = conn()
    try:
        return _rows(c.execute("SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                               (user_id, limit)).fetchall())
    finally:
        c.close()


def notified_since(user_id: str, kind: str, link: str, since: str) -> bool:
    """Whether this reminder already went to this person since `since` (worker de-duplication)."""
    c = conn()
    try:
        return c.execute("SELECT 1 FROM notifications WHERE user_id = ? AND kind = ? AND link = ? AND created_at >= ? LIMIT 1",
                         (user_id, kind, link, since)).fetchone() is not None
    finally:
        c.close()


def checkpoints_due(until: str) -> list[dict]:
    """Open check-ins of active probations due on or before `until`, with the employee's manager."""
    c = conn()
    try:
        return _rows(c.execute(
            """
            SELECT k.checkpoint_id, k.label, k.due_date, r.user_id, u.full_name, u.manager_id
            FROM probation_checkpoints k JOIN probation_records r ON r.probation_id = k.probation_id
            JOIN users u ON u.user_id = r.user_id
            WHERE r.status = 'active' AND k.completed_at IS NULL AND k.due_date <= ?
            """, (until,)).fetchall())
    finally:
        c.close()


def overdue_action_counts(today: str) -> list[tuple[str, int]]:
    c = conn()
    try:
        return [(r[0], r[1]) for r in c.execute(
            "SELECT user_id, COUNT(*) FROM development_actions WHERE status IN ('open', 'in_progress') "
            "AND due_date < ? GROUP BY user_id", (today,)).fetchall()]
    finally:
        c.close()


def unread_count(user_id: str) -> int:
    c = conn()
    try:
        return c.execute("SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read_at IS NULL", (user_id,)).fetchone()[0]
    finally:
        c.close()


def mark_notifications_read(user_id: str, notification_id: str | None = None) -> int:
    c = conn()
    try:
        cur = c.execute("UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL "
                        "AND (? IS NULL OR notification_id = ?)", (_now(), user_id, notification_id, notification_id))
        c.commit()
        return cur.rowcount
    finally:
        c.close()


def upsert_pulse(*, user_id: str, week: str, mood: int, workload: int, comment: str, shared: bool) -> None:
    c = conn()
    try:
        c.execute(
            """
            INSERT INTO pulse_checks (pulse_id, user_id, week, mood, workload, comment, shared, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, week) DO UPDATE SET mood = excluded.mood, workload = excluded.workload,
                comment = excluded.comment, shared = excluded.shared, created_at = excluded.created_at
            """, (_id("PLS"), user_id, week, mood, workload, comment, 1 if shared else 0, _now()))
        c.commit()
    finally:
        c.close()


def pulses_for_users(user_ids, since_week: str) -> dict[str, list[dict]]:
    c = conn()
    try:
        out: dict[str, list[dict]] = {}
        for r in c.execute("SELECT * FROM pulse_checks WHERE user_id IN (SELECT value FROM json_each(?)) AND week >= ? "
                           "ORDER BY week DESC", (_ids_param(user_ids), since_week)).fetchall():
            out.setdefault(r["user_id"], []).append(dict(r))
        return out
    finally:
        c.close()
