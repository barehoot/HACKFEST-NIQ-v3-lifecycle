"""Data-access layer for the real workflow state added in the merge with the
HACKFEST-NIQ agent backend: users/RBAC and the employee assessment-cycle
state machine (resume -> SME question approval -> MCQ -> case study ->
manager approval -> training).

This module is the ONLY writer to these tables (see scripts/seed_users.py
in the project root, which creates them). The backend process only ever reads
its own reference tables at request time, so there is no cross-process write
concurrency to manage here.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import bcrypt

# TALENT360_DB_PATH points the portal at another database file (a container
# volume, or the 20k-employee scale dataset); the default is the project's own.
DB_PATH = Path(os.getenv("TALENT360_DB_PATH") or Path(__file__).resolve().parents[2] / "db" / "talent360i.sqlite")

CYCLE_STATUSES = (
    "resume_uploaded",
    "pending_sme_setup",
    "pending_sme_approval",
    "ready_for_assessment",
    "mcq_submitted",
    "case_study_submitted",
    "pending_manager_approval",
    "manager_approved",
    "manager_rejected",
)


_WAL_READY: set[str] = set()
ROLES = ("employee", "sme", "scheduler", "manager", "leader", "admin", "hr")
_ROLE_CHECK_RE = re.compile(r"CHECK\s*\(\s*role\s+IN\s*\([^)]*\)\s*\)", re.IGNORECASE)


def ensure_user_roles() -> None:
    """Databases created before the HR role allow only six roles in users.role's CHECK.
    SQLite can't alter a CHECK, so the table is rebuilt once (the documented
    create-copy-drop-rename procedure), keeping every row, id and index."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'").fetchone()
        if not row or "'hr'" in row[0] or not _ROLE_CHECK_RE.search(row[0]):
            return
        check = "CHECK(role IN (" + ",".join(f"'{r}'" for r in ROLES) + "))"
        create = _ROLE_CHECK_RE.sub(check, row[0], count=1).replace("CREATE TABLE users", "CREATE TABLE users__roles", 1)
        create = create.replace('CREATE TABLE "users"', "CREATE TABLE users__roles", 1)
        indexes = [r[0] for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'users' AND sql IS NOT NULL")]
        conn.execute("PRAGMA foreign_keys = OFF")  # other tables reference users(user_id); ids are unchanged
        conn.execute("BEGIN IMMEDIATE")
        if "'hr'" in (conn.execute("SELECT sql FROM sqlite_master WHERE name = 'users'").fetchone() or [""])[0]:
            conn.rollback()  # another process migrated it while we waited for the lock
            return
        conn.execute(create)
        conn.execute("INSERT INTO users__roles SELECT * FROM users")
        conn.execute("DROP TABLE users")
        conn.execute("ALTER TABLE users__roles RENAME TO users")
        for statement in indexes:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


def get_conn() -> sqlite3.Connection:
    if str(DB_PATH) not in _WAL_READY:
        ensure_user_roles()
    # timeout = busy wait: a second writer (the lifecycle worker, another
    # portal worker) waits for the lock instead of failing at once.
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    if str(DB_PATH) not in _WAL_READY:
        # WAL (persistent, once per database file): readers never block the
        # writer, so dashboards stay responsive while snapshots refresh.
        conn.execute("PRAGMA journal_mode = WAL;")
        _WAL_READY.add(str(DB_PATH))
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def _rows(rows) -> list[dict]:
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Users / auth
# ---------------------------------------------------------------------------

def verify_user(username: str, password: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username.strip().lower(),)
        ).fetchone()
        if not row:
            return None
        if not bcrypt.checkpw(password.encode("utf-8"), row["password_hash"].encode("utf-8")):
            return None
        return _row(row)
    finally:
        conn.close()


def get_user(user_id: str) -> dict | None:
    conn = get_conn()
    try:
        return _row(conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone())
    finally:
        conn.close()


def list_users() -> list[dict]:
    conn = get_conn()
    try:
        return _rows(conn.execute("SELECT * FROM users ORDER BY role, full_name").fetchall())
    finally:
        conn.close()


def list_managers() -> list[dict]:
    conn = get_conn()
    try:
        return _rows(conn.execute("SELECT * FROM users WHERE role = 'manager' ORDER BY full_name").fetchall())
    finally:
        conn.close()


def create_user(*, username: str, email: str, password: str, full_name: str, role: str,
                 department: str = "", job_title: str = "", manager_id: str | None = None) -> dict:
    user_id = f"{role[:2].upper()}-{uuid.uuid4().hex[:8].upper()}"
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO users (user_id, username, email, password_hash, full_name, role, department, job_title, manager_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, username.strip().lower(), email.strip().lower(), password_hash, full_name, role,
             department, job_title, manager_id or None),
        )
        conn.commit()
        return get_user(user_id)
    finally:
        conn.close()


def update_user_role(user_id: str, role: str, manager_id: str | None = None) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE users SET role = ?, manager_id = ? WHERE user_id = ?",
            (role, manager_id or None, user_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Assessment cycles (the employee workflow state machine)
# ---------------------------------------------------------------------------

def _parse_cycle(cycle: dict | None) -> dict | None:
    if cycle is None:
        return None
    for field in ("mcq_score_json", "case_study_json", "case_study_answers_json", "evidence_report_json", "dev_plan_json"):
        raw = cycle.get(field)
        cycle[field[: -len("_json")]] = json.loads(raw) if raw else None
    return cycle


def get_active_cycle_for_user(user_id: str) -> dict | None:
    """The employee's most recent assessment cycle, or None if they've never uploaded a resume."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM assessment_cycles WHERE user_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return _parse_cycle(_row(row))
    finally:
        conn.close()


def get_cycle(cycle_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM assessment_cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()
        return _parse_cycle(_row(row))
    finally:
        conn.close()


def create_cycle(*, user_id: str, resume_filename: str, resume_text: str,
                  matched_role_id: str | None, matched_role_name: str | None,
                  matched_role_grade: str | None, match_status: str, status: str) -> str:
    cycle_id = f"CYC-{uuid.uuid4().hex[:10].upper()}"
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO assessment_cycles
                (cycle_id, user_id, resume_filename, resume_text, matched_role_id, matched_role_name,
                 matched_role_grade, match_status, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (cycle_id, user_id, resume_filename, resume_text, matched_role_id, matched_role_name,
             matched_role_grade, match_status, status, _now(), _now()),
        )
        conn.commit()
        return cycle_id
    finally:
        conn.close()


_UPDATABLE_CYCLE_COLUMNS = frozenset({
    "status", "match_status", "mcq_score_json", "case_study_json", "case_study_answers_json",
    "evidence_report_json", "dev_plan_json", "manager_decision", "manager_decision_notes", "manager_id",
})


def update_cycle(cycle_id: str, **fields) -> None:
    if not fields:
        return
    # Column names can't be bound as SQL parameters, so they are checked
    # against a fixed allowlist; every value is still a bound parameter.
    unknown = set(fields) - _UPDATABLE_CYCLE_COLUMNS
    if unknown:
        raise ValueError(f"update_cycle: unsupported column(s) {sorted(unknown)}")
    fields["updated_at"] = _now()
    columns = ", ".join(f"{k} = ?" for k in fields)
    conn = get_conn()
    try:
        # Allowlisted column names, bound values (bandit B608 reviewed).
        conn.execute(f"UPDATE assessment_cycles SET {columns} WHERE cycle_id = ?", (*fields.values(), cycle_id))  # nosec B608
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Generated questions (per-item SME approval)
# ---------------------------------------------------------------------------

def insert_generated_questions(cycle_id: str, items: list[dict]) -> None:
    conn = get_conn()
    try:
        for item in items:
            conn.execute(
                """
                INSERT INTO generated_questions
                    (question_id, cycle_id, skill, question_text, option_a, option_b, option_c, option_d,
                     correct_option, difficulty, is_critical, ai_confidence, sme_status, item_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    item["question_id"], cycle_id, item.get("skill"), item.get("question_text"),
                    item.get("option_a"), item.get("option_b"), item.get("option_c"), item.get("option_d"),
                    item.get("correct_option"), item.get("difficulty"),
                    1 if item.get("is_critical") else 0, item.get("ai_confidence"),
                    json.dumps(item),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def get_questions_for_cycle(cycle_id: str, sme_status: str | None = None) -> list[dict]:
    conn = get_conn()
    try:
        if sme_status:
            rows = conn.execute(
                "SELECT * FROM generated_questions WHERE cycle_id = ? AND sme_status = ? ORDER BY rowid",
                (cycle_id, sme_status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM generated_questions WHERE cycle_id = ? ORDER BY rowid", (cycle_id,)
            ).fetchall()
        return _rows(rows)
    finally:
        conn.close()


def get_pending_review_queue() -> list[dict]:
    """Cycles with at least one pending question, for the SME queue -- one row per pending question, joined with cycle/employee info."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT gq.*, ac.matched_role_name, ac.user_id AS employee_id, u.full_name AS employee_name
            FROM generated_questions gq
            JOIN assessment_cycles ac ON ac.cycle_id = gq.cycle_id
            JOIN users u ON u.user_id = ac.user_id
            WHERE gq.sme_status = 'pending'
            ORDER BY ac.created_at
            """
        ).fetchall()
        return _rows(rows)
    finally:
        conn.close()


def get_question(question_id: str) -> dict | None:
    conn = get_conn()
    try:
        return _row(conn.execute("SELECT * FROM generated_questions WHERE question_id = ?", (question_id,)).fetchone())
    finally:
        conn.close()


def decide_question(question_id: str, decision: str, reviewer_id: str) -> dict | None:
    """Approve or reject one generated question. Returns the (possibly updated) cycle dict.

    When this was the last pending question for its cycle, and at least one
    question is approved, the cycle advances to 'ready_for_assessment'.
    """
    conn = get_conn()
    try:
        question = _row(conn.execute("SELECT * FROM generated_questions WHERE question_id = ?", (question_id,)).fetchone())
        if not question:
            return None
        # Only a pending question can be decided: an approved question can't
        # be flipped after the employee may already have been served it.
        cur = conn.execute(
            "UPDATE generated_questions SET sme_status = ?, reviewed_by = ?, reviewed_at = ? "
            "WHERE question_id = ? AND sme_status = 'pending'",
            (decision, reviewer_id, _now(), question_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None

        cycle_id = question["cycle_id"]
        remaining_pending = conn.execute(
            "SELECT COUNT(*) FROM generated_questions WHERE cycle_id = ? AND sme_status = 'pending'", (cycle_id,)
        ).fetchone()[0]
        approved_count = conn.execute(
            "SELECT COUNT(*) FROM generated_questions WHERE cycle_id = ? AND sme_status = 'approved'", (cycle_id,)
        ).fetchone()[0]
        if remaining_pending == 0 and approved_count > 0:
            conn.execute(
                "UPDATE assessment_cycles SET status = 'ready_for_assessment', updated_at = ? WHERE cycle_id = ? AND status = 'pending_sme_approval'",
                (_now(), cycle_id),
            )
            conn.commit()
        return _parse_cycle(_row(conn.execute("SELECT * FROM assessment_cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# MCQ answers
# ---------------------------------------------------------------------------

def save_answers(cycle_id: str, answers: dict[str, str]) -> None:
    conn = get_conn()
    try:
        for question_id, selected in answers.items():
            conn.execute(
                """
                INSERT INTO assessment_answers (cycle_id, question_id, selected_option)
                VALUES (?, ?, ?)
                ON CONFLICT(cycle_id, question_id) DO UPDATE SET selected_option = excluded.selected_option
                """,
                (cycle_id, question_id, selected),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Cross-role queues
# ---------------------------------------------------------------------------

def get_scored_cycles() -> list[dict]:
    """Cycles with an MCQ score, for the SME's score-visibility queue."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT ac.*, u.full_name AS employee_name
            FROM assessment_cycles ac
            JOIN users u ON u.user_id = ac.user_id
            WHERE ac.mcq_score_json IS NOT NULL
            ORDER BY ac.updated_at DESC
            """
        ).fetchall()
        return [_parse_cycle(r) for r in _rows(rows)]
    finally:
        conn.close()


def get_manager_queue(manager_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT ac.*, u.full_name AS employee_name
            FROM assessment_cycles ac
            JOIN users u ON u.user_id = ac.user_id
            WHERE u.manager_id = ? AND ac.status = 'pending_manager_approval'
            ORDER BY ac.updated_at
            """,
            (manager_id,),
        ).fetchall()
        return [_parse_cycle(r) for r in _rows(rows)]
    finally:
        conn.close()


def save_pending_evidence(cycle_id: str, evidence_json: str) -> bool:
    """Stores a late Agent 4 report, only while the cycle still waits for its manager and
    has none. The cached development plan was built without it, so it is rebuilt."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE assessment_cycles SET evidence_report_json = ?, dev_plan_json = NULL, updated_at = ? "
            "WHERE cycle_id = ? AND status = 'pending_manager_approval' AND evidence_report_json IS NULL",
            (evidence_json, _now(), cycle_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def cycles_missing_evidence(limit: int) -> list[str]:
    conn = get_conn()
    try:
        return [r[0] for r in conn.execute(
            "SELECT cycle_id FROM assessment_cycles WHERE status = 'pending_manager_approval' "
            "AND evidence_report_json IS NULL AND case_study_json IS NOT NULL AND case_study_answers_json IS NOT NULL "
            "ORDER BY updated_at LIMIT ?", (limit,)).fetchall()]
    finally:
        conn.close()


def team_assessment_pipeline(manager_id: str, decided_limit: int = 8) -> dict:
    """A manager's direct reports' assessments that are not in the queue yet, and the
    latest decided ones -- so nothing a manager will review is invisible to them."""
    conn = get_conn()
    try:
        rows = _rows(conn.execute(
            """
            SELECT ac.*, u.full_name AS employee_name
            FROM assessment_cycles ac JOIN users u ON u.user_id = ac.user_id
            WHERE u.manager_id = ? AND ac.status NOT IN ('pending_manager_approval')
            ORDER BY ac.updated_at DESC
            """, (manager_id,)).fetchall())
    finally:
        conn.close()
    decided = [_parse_cycle(r) for r in rows if r["status"] in ("manager_approved", "manager_rejected")][:decided_limit]
    latest = {}
    for r in rows:  # newest first: one in-progress row per person
        if r["status"] not in ("manager_approved", "manager_rejected"):
            latest.setdefault(r["user_id"], r)
    return {"in_progress": [_parse_cycle(r) for r in latest.values()], "decided": decided}


def list_employees() -> list[dict]:
    conn = get_conn()
    try:
        return _rows(conn.execute(
            "SELECT user_id, full_name, job_title, manager_id FROM users WHERE role = 'employee' ORDER BY user_id"
        ).fetchall())
    finally:
        conn.close()


def decide_manager_review(cycle_id: str, decision: str, notes: str, manager_id: str,
                           dev_plan: dict | None = None) -> bool:
    status = "manager_approved" if decision == "approved" else "manager_rejected"
    conn = get_conn()
    try:
        # Status guard: a cycle can only be decided once the SME gate, MCQ and
        # evidence round are done -- not even an admin can skip those steps.
        cur = conn.execute(
            """
            UPDATE assessment_cycles
            SET status = ?, manager_decision = ?, manager_decision_notes = ?, manager_id = ?,
                dev_plan_json = COALESCE(?, dev_plan_json), updated_at = ?
            WHERE cycle_id = ? AND status = 'pending_manager_approval'
            """,
            (status, decision, notes, manager_id, json.dumps(dev_plan) if dev_plan is not None else None, _now(), cycle_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Scheduled assessments and training (the scheduler's calendar)
# ---------------------------------------------------------------------------

# Also in scripts/seed_users.py SCHEMA_STATEMENTS; created here too so a
# database built before the calendar existed picks the table up on first use.
SCHEDULED_EVENTS_SCHEMA = """
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
"""


def _schedule_conn() -> sqlite3.Connection:
    conn = get_conn()
    conn.execute(SCHEDULED_EVENTS_SCHEMA)
    return conn


def create_event(*, event_type: str, title: str, event_date: str, start_time: str | None = None,
                 duration_minutes: int | None = None, user_id: str | None = None, role_id: str | None = None,
                 skill: str | None = None, course_id: str | None = None, notes: str | None = None,
                 created_by: str | None = None) -> str:
    event_id = f"EVT-{uuid.uuid4().hex[:10].upper()}"
    conn = _schedule_conn()
    try:
        conn.execute(
            """
            INSERT INTO scheduled_events
                (event_id, event_type, title, user_id, role_id, skill, course_id, event_date,
                 start_time, duration_minutes, notes, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, event_type, title, user_id, role_id, skill, course_id, event_date,
             start_time, duration_minutes, notes, created_by, _now()),
        )
        conn.commit()
        return event_id
    finally:
        conn.close()


def list_events(start_date: str, end_date: str, *, user_id: str | None = None,
                include_cancelled: bool = False) -> list[dict]:
    """Events with start_date <= event_date <= end_date (ISO dates), with the employee's name."""
    conn = _schedule_conn()
    try:
        rows = conn.execute(
            """
            SELECT se.*, u.full_name AS employee_name, u.job_title AS employee_title
            FROM scheduled_events se
            LEFT JOIN users u ON u.user_id = se.user_id
            WHERE se.event_date BETWEEN ? AND ?
              AND (? IS NULL OR se.user_id = ?)
              AND (? OR se.status != 'cancelled')
            ORDER BY se.event_date, COALESCE(se.start_time, ''), se.created_at
            """,
            (start_date, end_date, user_id, user_id, 1 if include_cancelled else 0),
        ).fetchall()
        return _rows(rows)
    finally:
        conn.close()


def get_event(event_id: str) -> dict | None:
    conn = _schedule_conn()
    try:
        return _row(conn.execute("SELECT * FROM scheduled_events WHERE event_id = ?", (event_id,)).fetchone())
    finally:
        conn.close()


def scheduled_course_ids(user_id: str) -> set[str]:
    """Courses already booked (not cancelled) for this employee -- used to avoid double-booking."""
    conn = _schedule_conn()
    try:
        rows = conn.execute(
            "SELECT course_id FROM scheduled_events WHERE user_id = ? AND course_id IS NOT NULL AND status != 'cancelled'",
            (user_id,),
        ).fetchall()
        return {r["course_id"] for r in rows}
    finally:
        conn.close()


def last_training_date(user_id: str) -> str | None:
    """Date of this employee's latest still-booked training session, if any."""
    conn = _schedule_conn()
    try:
        row = conn.execute(
            "SELECT MAX(event_date) FROM scheduled_events WHERE user_id = ? AND event_type = 'training' AND status = 'scheduled'",
            (user_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def set_event_status(event_id: str, status: str) -> bool:
    conn = _schedule_conn()
    try:
        cur = conn.execute(
            "UPDATE scheduled_events SET status = ? WHERE event_id = ? AND status = 'scheduled'",
            (status, event_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_cycle_activity(start_date: str, end_date: str, user_id: str | None = None) -> list[dict]:
    """Assessment cycles last updated in the window -- the live resume-driven assessments."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT ac.cycle_id, ac.user_id, ac.matched_role_name, ac.status, ac.created_at, ac.updated_at,
                   u.full_name AS employee_name
            FROM assessment_cycles ac
            JOIN users u ON u.user_id = ac.user_id
            WHERE substr(ac.updated_at, 1, 10) BETWEEN ? AND ?
              AND (? IS NULL OR ac.user_id = ?)
            ORDER BY ac.updated_at
            """,
            (start_date, end_date, user_id, user_id),
        ).fetchall()
        return _rows(rows)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Leader calibration reviews of manager decisions
# ---------------------------------------------------------------------------

# Also in scripts/seed_users.py SCHEMA_STATEMENTS.
CALIBRATION_SCHEMA = """
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
"""


def _calibration_conn() -> sqlite3.Connection:
    conn = get_conn()
    conn.execute(CALIBRATION_SCHEMA)
    return conn


def list_decided_cycles() -> list[dict]:
    """Every cycle a manager has decided -- including ones a leader has since reopened for re-review."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT ac.*, u.full_name AS employee_name, u.manager_id AS line_manager_id,
                   COALESCE(ac.manager_id, u.manager_id) AS decided_by,
                   m.full_name AS manager_name
            FROM assessment_cycles ac
            JOIN users u ON u.user_id = ac.user_id
            LEFT JOIN users m ON m.user_id = COALESCE(ac.manager_id, u.manager_id)
            WHERE ac.manager_decision IS NOT NULL
            ORDER BY ac.updated_at DESC
            """
        ).fetchall()
        return [_parse_cycle(r) for r in _rows(rows)]
    finally:
        conn.close()


def list_calibration_reviews(cycle_id: str | None = None) -> list[dict]:
    conn = _calibration_conn()
    try:
        rows = conn.execute(
            """
            SELECT cr.*, l.full_name AS leader_name, m.full_name AS manager_name
            FROM calibration_reviews cr
            LEFT JOIN users l ON l.user_id = cr.leader_id
            LEFT JOIN users m ON m.user_id = cr.manager_id
            WHERE (? IS NULL OR cr.cycle_id = ?)
            ORDER BY cr.created_at DESC, cr.rowid DESC
            """,
            (cycle_id, cycle_id),
        ).fetchall()
        return _rows(rows)
    finally:
        conn.close()


def add_calibration_review(cycle_id: str, leader_id: str, outcome: str, note: str) -> bool:
    """Records a leader's review of a decided cycle. 'rereview' returns the
    cycle to its manager's validation queue (the manager decides again --
    the leader never overrides the decision). Only decided cycles qualify."""
    conn = _calibration_conn()
    try:
        cycle = _row(conn.execute("SELECT * FROM assessment_cycles WHERE cycle_id = ?", (cycle_id,)).fetchone())
        if not cycle or cycle["status"] not in ("manager_approved", "manager_rejected") or not cycle["manager_decision"]:
            return False
        conn.execute(
            """
            INSERT INTO calibration_reviews
                (review_id, cycle_id, leader_id, manager_id, manager_decision, manager_note, outcome, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (f"CAL-{uuid.uuid4().hex[:10].upper()}", cycle_id, leader_id, cycle["manager_id"],
             cycle["manager_decision"], cycle["manager_decision_notes"], outcome, note, _now()),
        )
        if outcome == "rereview":
            conn.execute(
                "UPDATE assessment_cycles SET status = 'pending_manager_approval', updated_at = ? "
                "WHERE cycle_id = ? AND status IN ('manager_approved', 'manager_rejected')",
                (_now(), cycle_id),
            )
        conn.commit()
        return True
    finally:
        conn.close()


def list_cycles_for_user(user_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM assessment_cycles WHERE user_id = ? ORDER BY created_at DESC, rowid DESC", (user_id,)
        ).fetchall()
        return [_parse_cycle(r) for r in _rows(rows)]
    finally:
        conn.close()


def list_all_cycles() -> list[dict]:
    conn = get_conn()
    try:
        return _rows(conn.execute(
            "SELECT cycle_id, user_id, status, manager_decision, manager_id, created_at, updated_at FROM assessment_cycles"
        ).fetchall())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Question bank: SME-approved questions, reusable across assessments
# ---------------------------------------------------------------------------

# Also in scripts/seed_users.py SCHEMA_STATEMENTS.
BANK_SCHEMA = """
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
"""

# Every question an SME approved inside an employee's assessment is also a
# bank question (copies deployed from the bank carry bank_question_id and
# are not re-added). Idempotent, so it runs on every bank access.
_BANK_BACKFILL = """
    INSERT OR IGNORE INTO bank_questions
        (question_id, role_id, role_name, skill, target_level, question_text, option_a, option_b, option_c, option_d,
         correct_option, difficulty, is_critical, ai_confidence, sme_status, reviewed_by, reviewed_at, item_json,
         source, created_by, created_at, times_used, last_used_at)
    SELECT gq.question_id, COALESCE(json_extract(gq.item_json, '$.role_id'), ac.matched_role_id),
           COALESCE(json_extract(gq.item_json, '$.role_name'), ac.matched_role_name), gq.skill,
           json_extract(gq.item_json, '$.target_proficiency_level'), gq.question_text,
           gq.option_a, gq.option_b, gq.option_c, gq.option_d, gq.correct_option, gq.difficulty, gq.is_critical,
           gq.ai_confidence, 'approved', gq.reviewed_by, gq.reviewed_at, gq.item_json,
           'cycle', gq.reviewed_by, COALESCE(gq.reviewed_at, ac.created_at), 1, ac.updated_at
    FROM generated_questions gq
    JOIN assessment_cycles ac ON ac.cycle_id = gq.cycle_id
    WHERE gq.sme_status = 'approved' AND json_extract(gq.item_json, '$.bank_question_id') IS NULL
      AND COALESCE(json_extract(gq.item_json, '$.role_id'), ac.matched_role_id) IS NOT NULL
"""


def _bank_conn() -> sqlite3.Connection:
    conn = get_conn()
    conn.execute(BANK_SCHEMA)
    conn.execute(_BANK_BACKFILL)
    conn.commit()
    return conn


def list_bank(role_id: str | None = None, status: str | None = None) -> list[dict]:
    conn = _bank_conn()
    try:
        return _rows(conn.execute(
            """
            SELECT bq.*, u.full_name AS reviewer_name
            FROM bank_questions bq LEFT JOIN users u ON u.user_id = bq.reviewed_by
            WHERE (? IS NULL OR bq.role_id = ?) AND (? IS NULL OR bq.sme_status = ?)
            ORDER BY bq.role_id, bq.skill, bq.target_level, bq.created_at
            """,
            (role_id, role_id, status, status),
        ).fetchall())
    finally:
        conn.close()


def get_bank_question(question_id: str) -> dict | None:
    conn = _bank_conn()
    try:
        return _row(conn.execute("SELECT * FROM bank_questions WHERE question_id = ?", (question_id,)).fetchone())
    finally:
        conn.close()


def insert_bank_questions(items: list[dict], created_by: str) -> list[str]:
    """New AI-generated bank questions, pending SME review."""
    conn = _bank_conn()
    try:
        ids = []
        for item in items:
            conn.execute(
                """
                INSERT INTO bank_questions
                    (question_id, role_id, role_name, skill, target_level, question_text, option_a, option_b, option_c,
                     option_d, correct_option, difficulty, is_critical, ai_confidence, sme_status, item_json, source,
                     created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, 'generated', ?, ?)
                """,
                (item["question_id"], item.get("role_id"), item.get("role_name"), item.get("skill"),
                 item.get("target_proficiency_level"), item.get("question_text"), item.get("option_a"),
                 item.get("option_b"), item.get("option_c"), item.get("option_d"), item.get("correct_option"),
                 item.get("difficulty"), 1 if item.get("is_critical") else 0, item.get("ai_confidence"),
                 json.dumps(item), created_by, _now()),
            )
            ids.append(item["question_id"])
        conn.commit()
        return ids
    finally:
        conn.close()


def decide_bank_question(question_id: str, decision: str, reviewer_id: str) -> bool:
    conn = _bank_conn()
    try:
        cur = conn.execute(
            "UPDATE bank_questions SET sme_status = ?, reviewed_by = ?, reviewed_at = ? "
            "WHERE question_id = ? AND sme_status = 'pending'",
            (decision, reviewer_id, _now(), question_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def attach_bank_questions(cycle_id: str, bank_rows: list[dict], deployed_by: str | None) -> list[str]:
    """Deploys approved bank questions into a cycle as already-approved copies,
    keeping the original SME approval and a link back to the bank question."""
    conn = _bank_conn()
    try:
        ids = []
        for bank in bank_rows:
            question_id = f"BQ-{uuid.uuid4().hex[:10].upper()}"
            item = json.loads(bank["item_json"]) if bank.get("item_json") else {}
            item.update(question_id=question_id, bank_question_id=bank["question_id"], deployed_by=deployed_by)
            conn.execute(
                """
                INSERT INTO generated_questions
                    (question_id, cycle_id, skill, question_text, option_a, option_b, option_c, option_d,
                     correct_option, difficulty, is_critical, ai_confidence, sme_status, reviewed_by, reviewed_at, item_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'approved', ?, ?, ?)
                """,
                (question_id, cycle_id, bank["skill"], bank["question_text"], bank["option_a"], bank["option_b"],
                 bank["option_c"], bank["option_d"], bank["correct_option"], bank["difficulty"], bank["is_critical"],
                 bank["ai_confidence"], bank["reviewed_by"], bank["reviewed_at"], json.dumps(item)),
            )
            conn.execute(
                "UPDATE bank_questions SET times_used = times_used + 1, last_used_at = ? WHERE question_id = ?",
                (_now(), bank["question_id"]),
            )
            ids.append(question_id)
        conn.commit()
        return ids
    finally:
        conn.close()


def list_roles() -> list[dict]:
    conn = get_conn()
    try:
        return _rows(conn.execute("SELECT role_id, role_name, tower, role_grade FROM role_master ORDER BY tower, role_name").fetchall())
    finally:
        conn.close()


def role_skills(role_id: str) -> list[dict]:
    conn = get_conn()
    try:
        return _rows(conn.execute(
            "SELECT skill, target_proficiency_level AS target_level, is_critical FROM role_skill_map "
            "WHERE role_id = ? ORDER BY is_critical DESC, target_proficiency_level DESC, skill",
            (role_id,),
        ).fetchall())
    finally:
        conn.close()


def role_by_name(role_name: str | None) -> dict | None:
    conn = get_conn()
    try:
        return _row(conn.execute(
            "SELECT role_id, role_name, tower, role_grade FROM role_master WHERE lower(role_name) = lower(?)",
            (role_name or "",),
        ).fetchone())
    finally:
        conn.close()


def cycles_in_status(status: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT ac.*, u.full_name AS employee_name FROM assessment_cycles ac
            JOIN users u ON u.user_id = ac.user_id WHERE ac.status = ? ORDER BY ac.created_at
            """,
            (status,),
        ).fetchall()
        return [_parse_cycle(r) for r in _rows(rows)]
    finally:
        conn.close()


def import_bank_questions(items: list[dict]) -> int:
    """Loads SME-reviewed questions from the hackfest dataset (Assessment_QBank)
    into the bank. Idempotent: rows already in the bank are left alone, so an
    SME's later decision on one is never overwritten."""
    conn = _bank_conn()
    try:
        added = 0
        for item in items:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO bank_questions
                    (question_id, role_id, role_name, skill, target_level, question_text, option_a, option_b, option_c,
                     option_d, correct_option, difficulty, is_critical, ai_confidence, sme_status, item_json, source,
                     created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'dataset', NULL, ?)
                """,
                (item["question_id"], item["role_id"], item.get("role_name"), item["skill"],
                 item.get("target_proficiency_level"), item["question_text"], item["option_a"], item["option_b"],
                 item["option_c"], item["option_d"], item["correct_option"], item.get("difficulty"),
                 1 if item.get("is_critical") else 0, item.get("ai_confidence"), item["sme_status"],
                 json.dumps(item), _now()),
            )
            added += cur.rowcount
        conn.commit()
        return added
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Progression: manager-reviewed skill levels, AI training outlines, promotion
# nominations. Also in scripts/seed_users.py SCHEMA_STATEMENTS.
#
# skill_certifications.basis:
#   certified -- the manager approved the assessment and certified this level
#   assessed  -- the manager confirmed a gap: the level the assessment showed
#                for a skill that fell short (reject, or an un-ticked weak skill)
# ---------------------------------------------------------------------------

PROGRESSION_SCHEMA = (
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
)


def _progression_conn() -> sqlite3.Connection:
    conn = get_conn()
    for statement in PROGRESSION_SCHEMA:
        conn.execute(statement)
    return conn


def add_certifications(*, user_id: str, role_id: str, cycle_id: str, certified_by: str,
                       levels: list[tuple[str, int, int | None]], basis: str = "certified") -> int:
    """Records manager-reviewed levels from one assessment: (skill, level, previous_level)."""
    conn = _progression_conn()
    try:
        for skill, level, previous in levels:
            conn.execute(
                """
                INSERT INTO skill_certifications
                    (cert_id, user_id, role_id, skill, level, previous_level, basis, cycle_id, certified_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (f"CERT-{uuid.uuid4().hex[:10].upper()}", user_id, role_id, skill, level, previous, basis, cycle_id,
                 certified_by, _now()),
            )
        conn.commit()
        return len(levels)
    finally:
        conn.close()


def list_certifications(user_ids: list[str] | None = None, cycle_id: str | None = None,
                        basis: str | None = "certified") -> list[dict]:
    """Active manager-reviewed levels, newest first (certified only, unless basis=None or "assessed")."""
    conn = _progression_conn()
    try:
        return _rows(conn.execute(
            """
            SELECT sc.*, u.full_name AS certified_by_name FROM skill_certifications sc
            LEFT JOIN users u ON u.user_id = sc.certified_by
            WHERE sc.status = 'active'
              AND (? IS NULL OR sc.user_id IN (SELECT value FROM json_each(?)))
              AND (? IS NULL OR sc.cycle_id = ?) AND (? IS NULL OR sc.basis = ?)
            ORDER BY sc.created_at DESC, sc.rowid DESC
            """,
            (None if user_ids is None else 1, json.dumps(user_ids or []), cycle_id, cycle_id, basis, basis),
        ).fetchall())
    finally:
        conn.close()


def revoke_certifications(cycle_id: str) -> int:
    conn = _progression_conn()
    try:
        cur = conn.execute("UPDATE skill_certifications SET status = 'revoked' WHERE cycle_id = ? AND status = 'active'",
                           (cycle_id,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def _parse_outline(row: dict | None) -> dict | None:
    if row is None:
        return None
    for field in ("items_json", "outline_json", "event_ids_json"):
        raw = row.get(field)
        row[field[: -len("_json")]] = json.loads(raw) if raw else None
    return row


def create_outline(*, cycle_id: str, user_id: str, mode: str, items: list[dict], created_by: str) -> str:
    outline_id = f"OUT-{uuid.uuid4().hex[:10].upper()}"
    conn = _progression_conn()
    try:
        conn.execute(
            """
            INSERT INTO training_outlines (outline_id, cycle_id, user_id, mode, items_json, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (outline_id, cycle_id, user_id, mode, json.dumps(items), created_by, _now(), _now()),
        )
        conn.commit()
        return outline_id
    finally:
        conn.close()


def set_outline_content(outline_id: str, *, outline: dict | None = None, event_ids: list[str] | None = None) -> None:
    conn = _progression_conn()
    try:
        if outline is not None:
            conn.execute("UPDATE training_outlines SET outline_json = ?, updated_at = ? WHERE outline_id = ?",
                         (json.dumps(outline), _now(), outline_id))
        if event_ids is not None:
            conn.execute("UPDATE training_outlines SET event_ids_json = ?, updated_at = ? WHERE outline_id = ?",
                         (json.dumps(event_ids), _now(), outline_id))
        conn.commit()
    finally:
        conn.close()


def list_outlines(*, user_id: str | None = None, cycle_id: str | None = None, include_superseded: bool = False) -> list[dict]:
    """Training outlines, newest first."""
    conn = _progression_conn()
    try:
        rows = conn.execute(
            """
            SELECT o.*, u.full_name AS created_by_name FROM training_outlines o
            LEFT JOIN users u ON u.user_id = o.created_by
            WHERE (? IS NULL OR o.user_id = ?) AND (? IS NULL OR o.cycle_id = ?) AND (? OR o.status = 'active')
            ORDER BY o.created_at DESC, o.rowid DESC
            """,
            (user_id, user_id, cycle_id, cycle_id, 1 if include_superseded else 0),
        ).fetchall()
        return [_parse_outline(r) for r in _rows(rows)]
    finally:
        conn.close()


def supersede_outlines(cycle_id: str) -> list[dict]:
    """Retires a cycle's outlines (a leader reopened the decision); returns them so their sessions can be cancelled."""
    outlines = list_outlines(cycle_id=cycle_id)
    conn = _progression_conn()
    try:
        conn.execute("UPDATE training_outlines SET status = 'superseded', updated_at = ? WHERE cycle_id = ? AND status = 'active'",
                     (_now(), cycle_id))
        conn.commit()
    finally:
        conn.close()
    return outlines


def create_nomination(*, user_id: str, from_role_id: str | None, to_role_id: str, profile: dict,
                      manager_id: str, manager_note: str) -> str | None:
    """A manager's promotion nomination. None if one is already pending for this person."""
    conn = _progression_conn()
    try:
        if conn.execute("SELECT 1 FROM promotion_nominations WHERE user_id = ? AND status = 'pending'", (user_id,)).fetchone():
            return None
        nomination_id = f"NOM-{uuid.uuid4().hex[:10].upper()}"
        conn.execute(
            """
            INSERT INTO promotion_nominations
                (nomination_id, user_id, from_role_id, to_role_id, profile_json, manager_id, manager_note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (nomination_id, user_id, from_role_id, to_role_id, json.dumps(profile), manager_id, manager_note, _now()),
        )
        conn.commit()
        return nomination_id
    finally:
        conn.close()


def list_nominations(*, status: str | None = None, user_ids: list[str] | None = None) -> list[dict]:
    conn = _progression_conn()
    try:
        rows = conn.execute(
            """
            SELECT n.*, u.full_name AS employee_name, m.full_name AS manager_name, l.full_name AS leader_name,
                   fr.role_name AS from_role_name, tr.role_name AS to_role_name, tr.role_grade AS to_role_grade
            FROM promotion_nominations n
            JOIN users u ON u.user_id = n.user_id
            LEFT JOIN users m ON m.user_id = n.manager_id
            LEFT JOIN users l ON l.user_id = n.leader_id
            LEFT JOIN role_master fr ON fr.role_id = n.from_role_id
            LEFT JOIN role_master tr ON tr.role_id = n.to_role_id
            WHERE (? IS NULL OR n.status = ?)
              AND (? IS NULL OR n.user_id IN (SELECT value FROM json_each(?)))
            ORDER BY CASE n.status WHEN 'pending' THEN 0 ELSE 1 END, n.created_at DESC
            """,
            (status, status, None if user_ids is None else 1, json.dumps(user_ids or [])),
        ).fetchall()
        out = _rows(rows)
        for row in out:
            row["profile"] = json.loads(row["profile_json"])
        return out
    finally:
        conn.close()


def decide_nomination(nomination_id: str, decision: str, leader_id: str, note: str) -> bool:
    conn = _progression_conn()
    try:
        cur = conn.execute(
            "UPDATE promotion_nominations SET status = ?, leader_id = ?, leader_note = ?, decided_at = ? "
            "WHERE nomination_id = ? AND status = 'pending'",
            (decision, leader_id, note, _now(), nomination_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def role_skill_details(role_id: str) -> list[dict]:
    """A role's skills with their target level and the target level's indicator text."""
    conn = get_conn()
    try:
        return _rows(conn.execute(
            """
            SELECT rsm.skill_id, rsm.skill, rsm.capability, rsm.target_proficiency_level AS target_level,
                   rsm.is_critical, rsm.behavior_indicator,
                   sm.l1_indicator, sm.l2_indicator, sm.l3_indicator, sm.l4_indicator, sm.l5_indicator
            FROM role_skill_map rsm LEFT JOIN skill_master sm ON sm.skill_id = rsm.skill_id
            WHERE rsm.role_id = ?
            ORDER BY rsm.is_critical DESC, rsm.target_proficiency_level DESC, rsm.skill
            """,
            (role_id,),
        ).fetchall())
    finally:
        conn.close()


def role_master_rows() -> list[dict]:
    conn = get_conn()
    try:
        return _rows(conn.execute(
            "SELECT role_id, tower, process_group, role_name, role_grade FROM role_master ORDER BY tower, role_id"
        ).fetchall())
    finally:
        conn.close()


def tni_role_ids(user_ids: list[str]) -> dict[str, str]:
    """user_id -> the role their Skill_Gaps_TNI rows are for."""
    conn = get_conn()
    try:
        return {r["user_id"]: r["role_id"] for r in conn.execute(
            "SELECT user_id, MIN(role_id) AS role_id FROM skill_gaps "
            "WHERE user_id IN (SELECT value FROM json_each(?)) GROUP BY user_id",
            (json.dumps(user_ids),),
        ).fetchall()}
    finally:
        conn.close()


def seen_bank_questions(user_id: str) -> set[str]:
    """Bank question ids already deployed to this employee in any assessment."""
    conn = get_conn()
    try:
        return {r[0] for r in conn.execute(
            """
            SELECT DISTINCT json_extract(gq.item_json, '$.bank_question_id') FROM generated_questions gq
            JOIN assessment_cycles ac ON ac.cycle_id = gq.cycle_id
            WHERE ac.user_id = ? AND json_extract(gq.item_json, '$.bank_question_id') IS NOT NULL
            """,
            (user_id,),
        ).fetchall()}
    finally:
        conn.close()


def cycle_question_levels(cycle_id: str) -> dict[str, int]:
    """skill -> the highest proficiency level this assessment's approved questions tested."""
    levels: dict[str, int] = {}
    for q in get_questions_for_cycle(cycle_id, sme_status="approved"):
        item = json.loads(q["item_json"]) if q.get("item_json") else {}
        level = item.get("target_proficiency_level") or item.get("target_level")
        if q["skill"] and level:
            levels[q["skill"]] = max(levels.get(q["skill"], 0), int(level))
    return levels


def complete_event(event_id: str, on_date: str) -> bool:
    conn = _schedule_conn()
    try:
        cur = conn.execute(
            "UPDATE scheduled_events SET status = 'completed', event_date = ? WHERE event_id = ? AND status = 'scheduled'",
            (on_date, event_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
