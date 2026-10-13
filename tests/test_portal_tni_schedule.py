"""
Portal TNI, SME review and scheduler-calendar tests:

  - TNI for a synthetic-cohort employee: heatmap, skill set, learning curve,
    recommended learning with gap coverage (and the course-matching rules
    that work around the dataset's shifted R2R course ids)
  - manager Learning & TNI built from real direct reports
  - SME: every review link resolves -- question-bank ids used to 404
  - scheduler: book / auto-schedule / cancel on the calendar, with the
    employee seeing their own bookings
  - employee Assessments tab and personal calendar
  - manager training enrolment (own team only)
  - leader organisation report / CSV export and calibration re-review
  - question bank reuse, SME assessment builder / deploy, automatic baseline
    assignment, and the AI-use flag shown to the manager

Runs against a throwaway SQLite DB with a tiny hand-built reference dataset;
no network, LLM key or dataset build needed.
"""
import importlib.util
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import bcrypt
import pytest

from test_portal_workflow import _ENV, _purge_portal_modules

ROOT = Path(__file__).resolve().parent.parent
PORTAL = ROOT / "Talent360i_phase11C_fixed_remote_hybrid"
PASSWORD = "Tni-Schedule-Test-Pass-42!"
CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')

USERS = [  # user_id, username, role, manager_id[, full_name]
    ("M-ONE", "mgr.one", "manager", None),
    ("M-TWO", "mgr.two", "manager", None),
    ("E-TNI", "emp.tni", "employee", "M-ONE"),
    ("E-NEW", "emp.new", "employee", "M-ONE"),
    ("E-BASE", "emp.base", "employee", "M-ONE"),
    ("E-EVIL", "emp.evil", "employee", "M-ONE", "=HYPERLINK(\"http://x\")"),
    ("E-OTHER", "emp.other", "employee", "M-TWO"),
    ("S-ONE", "sme.one", "sme", None),
    ("SC-ONE", "sched.one", "scheduler", None),
    ("L-ONE", "leader.one", "leader", None),
]
ROLE = "R2R_test_role"
REFERENCE = {
    "role_master": [(ROLE, "R2R", "Test", "Test Preparer", "B3", "B3")],
    "role_skill_map": [
        ("RSM-1", ROLE, "SK_ALPHA", "Skill Alpha", "Technical Expertise", 4, "Advanced", "ind", 1, "question", 0),
        ("RSM-2", ROLE, "SK_BETA", "Skill Beta", "Technical Expertise", 3, "Intermediate", "ind", 0, "question", 0),
        ("RSM-3", ROLE, "SK_GAMMA", "Skill Gamma", "Foundational", 3, "Intermediate", "ind", 0, "question", 0),
        ("RSM-4", ROLE, "SK_DELTA", "Skill Delta", "Foundational", 3, "Intermediate", "ind", 0, "question", 0),
    ],
    "training_catalogue": [
        ("TRN-A", "R2R", "Role-based assessment remediation for Skill Alpha", "Lab", "Gen", "High", "SDL"),
        ("TRN-B", "R2R", "Role-based assessment remediation for Skill Gamma", "Lab", "Gen", "High", "SDL"),
        ("TRN-X", "R2R", "Role-based assessment remediation for ", "Lab", "Gen", "High", "SDL"),
        ("TRN-N", "R2R", "(Title currently reads CI—moving under CI below)", "Lab", "Gen", "High", "SDL"),
    ],
    "training_skill_map": [
        ("TSM-1", "TRN-B", "R2R", "SK_ALPHA", None, "Technical Expertise", "Gen", "High"),
        ("TSM-2", "TRN-A", "R2R", "SK_ALPHA", None, "Technical Expertise", "Gen", "High"),
        ("TSM-3", "TRN-N", "R2R", "SK_BETA", "Skill Beta", "Technical Expertise", "Gen", "High"),
    ],
    # gap_id, user_id, role_id, team_id, skill_id, skill, capability, target, current, formula, severity, rec, tni, xfn
    "skill_gaps": [
        # Recommended id is the shifted one (TRN-B is Skill Gamma's course); TRN-A names the skill.
        ("G-1", "E-TNI", ROLE, "T", "SK_ALPHA", "Skill Alpha", None, 4, 1.0, "3", "High", "TRN-B", "Assign", None),
        # Only an unfilled and a note-only title exist -> not coverable.
        ("G-2", "E-TNI", ROLE, "T", "SK_BETA", "Skill Beta", None, 3, 2.0, "1", "Moderate", "TRN-X", "Assign", None),
        ("G-3", "E-TNI", ROLE, "T", "SK_GAMMA", "Skill Gamma", None, 3, 3.0, "0", "No gap", "TRN-B", "No action", None),
        ("G-4", "E-TNI", ROLE, "T", "SK_DELTA", "Skill Delta", None, 3, None, "Assessment Pending", None, None, None, None),
        ("G-5", "E-NEW", ROLE, "T", "SK_ALPHA", "Skill Alpha", None, 4, None, "Assessment Pending", None, None, None, None),
    ],
}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def portal(tmp_path_factory):
    import os

    from fastapi.testclient import TestClient

    tmp = tmp_path_factory.mktemp("tni")
    saved_env = {k: os.environ.get(k) for k in [*_ENV, "TALENT360_LOG_DIR"]}
    os.environ.update({**_ENV, "TALENT360_DEMO_PASSWORD": PASSWORD, "TALENT360_SYNTHETIC_PASSWORD": PASSWORD})
    os.environ.setdefault("TALENT360_LOG_DIR", str(tmp / "logs"))
    _purge_portal_modules()
    sys.path.insert(0, str(PORTAL))
    try:
        seed = _load("seed_users_for_tni_test", ROOT / "scripts" / "seed_users.py")
        reference = _load("build_sqlite_for_tni_test", ROOT / "scripts" / "build_sqlite.py")
        db_file = tmp / "talent360i.sqlite"
        conn = sqlite3.connect(db_file)
        for statement in reference.SCHEMA_STATEMENTS + seed.SCHEMA_STATEMENTS:
            conn.execute(statement)
        for table, rows in REFERENCE.items():
            marks = ",".join("?" * len(rows[0]))
            conn.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)  # nosec B608 -- fixed test table names
        pw_hash = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
        for user_id, username, role, manager_id, *name in USERS:
            conn.execute(
                "INSERT INTO users (user_id, username, email, password_hash, full_name, role, job_title, manager_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, username, f"{username}@test.local", pw_hash, name[0] if name else username.replace(".", " ").title(),
                 role, "Test Preparer", manager_id),
            )
        conn.commit()
        conn.close()

        app_module = _load("portal_app", PORTAL / "app.py")
        from services import db, schedule, tni

        db.DB_PATH = db_file

        def login(username):
            client = TestClient(app_module.app, follow_redirects=False)
            token = CSRF.search(client.get("/login").text).group(1)
            r = client.post("/login", data={"username": username, "password": PASSWORD, "csrf_token": token})
            assert r.status_code == 303 and r.headers["location"] != "/login", username
            return client

        yield type("Portal", (), {"login": staticmethod(login), "db": db, "tni": tni, "schedule": schedule})
    finally:
        sys.path.remove(str(PORTAL))
        _purge_portal_modules()
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _token(client, page):
    r = client.get(page)
    assert r.status_code == 200, (page, r.status_code)
    return CSRF.search(r.text).group(1), r.text


def _future(days):
    """A weekday at least `days` ahead, so business-day stepping can't move it."""
    day = date.today() + timedelta(days=days)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


# ---------------------------------------------------------------------------
# TNI read model
# ---------------------------------------------------------------------------

def test_course_matching_prefers_title_and_skips_placeholders(portal):
    plan = portal.tni.employee_tni("E-TNI")
    by_skill = {s["skill"]: s for s in plan["skills"]}
    # The shifted TNI recommendation (TRN-B) loses to the course that names the skill.
    assert by_skill["Skill Alpha"]["course"]["course_id"] == "TRN-A"
    # Unfilled / note-only titles are never offered, so this gap stays uncovered.
    assert by_skill["Skill Beta"]["course"] is None
    assert [s["skill"] for s in plan["uncovered"]] == ["Skill Beta"]
    assert by_skill["Skill Delta"]["status"] == "Not assessed"
    summary = plan["summary"]
    assert (summary["gaps"], summary["covered"], summary["coverage_pct"], summary["not_assessed"]) == (2, 1, 50, 1)


def test_learning_curve_follows_the_skill_up_path(portal):
    plan = portal.tni.employee_tni("E-TNI")
    # High + critical gap first (1.5 + 1 + 0.5 = 3 wks), then the moderate one (1.5 wks).
    assert [(s["skill"], s["start_week"], s["end_week"]) for s in plan["path"]] == [
        ("Skill Alpha", 0.0, 3.0), ("Skill Beta", 3.0, 4.5)]
    points = plan["curve"]["points"]
    assert points[0]["y"] == pytest.approx((1 + 2 + 3) / 3, abs=0.01)  # assessed skills only
    assert points[-1]["x"] == 4.5 and points[-1]["y"] == pytest.approx((4 + 3 + 3) / 3, abs=0.01)
    assert all(a["y"] <= b["y"] for a, b in zip(points, points[1:]))
    assert len(points) == 1 + 3 + 1  # one point per level gained


def test_employee_without_tni_rows_gets_their_role_skills_unassessed(portal):
    plan = portal.tni.employee_tni("E-BASE")  # no Skill_Gaps_TNI rows; job title names the role
    assert plan["role_id"] == ROLE and plan["summary"]["total"] == 4 and plan["summary"]["assessed"] == 0
    assert all(s["status"] == "Not assessed" for s in plan["skills"]) and plan["curve"] is None
    assert portal.tni.employee_tni("NOBODY") is None


# ---------------------------------------------------------------------------
# Employee and manager pages
# ---------------------------------------------------------------------------

def test_employee_skills_page_has_heatmap_curve_and_skill_set(portal):
    page = portal.login("emp.tni").get("/employee/skills").text
    assert page.count('class="heat-cell') == 4 * 5
    assert page.count('class="heat-cell gap"') == 3 + 1  # L2-L4 for Alpha, L3 for Beta
    assert 'id="my-curve-data"' in page and "Skill set" in page
    filtered = portal.login("emp.tni").get("/employee/skills?status=Not%20assessed").text
    assert "Skill Delta" in filtered and "Skill Alpha</b>" not in filtered


def test_training_plan_no_longer_404s_and_shows_recommended_learning(portal):
    page = portal.login("emp.tni").get("/employee/learning")
    assert page.status_code == 200
    assert "TRN-A" in page.text and "Skill-up path" in page.text and "50%" in page.text
    # Someone with every skill still unassessed gets a clear state, not a 404.
    fresh = portal.login("emp.new").get("/employee/learning")
    assert fresh.status_code == 200 and "Assessment pending" in fresh.text


def test_catalogue_course_page_resolves_real_course_ids(portal):
    client = portal.login("emp.tni")
    assert client.get("/learning/catalog/TRN-A").status_code == 200
    assert client.get("/learning/catalog/TRN-X").status_code == 404  # unfilled title
    assert client.get("/learning/catalog/NOPE").status_code == 404


def test_manager_tni_uses_real_direct_reports(portal):
    mgr = portal.login("mgr.one")
    page = mgr.get(f"/manager/learning?role={ROLE}").text
    assert "Emp Tni" in page and "Emp New" in page and "Emp Base" in page
    assert page.count('class="team-cell') == 4 * 4  # every M-ONE report, TNI rows or role skills
    assert "Emp Other" not in page  # M-TWO's report
    assert 'id="team-curve-data"' in page
    assert mgr.get("/manager/learning?role=<script>").status_code == 200  # unknown role falls back
    gaps = mgr.get("/manager/skill-gaps").text
    assert "Skill Alpha" in gaps and "TRN-A" in gaps
    assert mgr.get("/manager/dashboard").status_code == 200


# ---------------------------------------------------------------------------
# SME review links
# ---------------------------------------------------------------------------

def test_every_sme_review_link_resolves_and_bank_questions_can_be_decided(portal):
    sme = portal.login("sme.one")
    dashboard = sme.get("/sme/dashboard").text
    links = set(re.findall(r'href="(/sme/questions/[^"]+)"', dashboard))
    assert links, "dashboard should list pending bank questions"
    for link in links:
        assert sme.get(link).status_code == 200, link
    link = sorted(links)[0]
    token, _ = _token(sme, link)
    assert sme.post(link + "/approve", data={"csrf_token": token}).status_code == 303
    assert sme.post(link + "/approve", data={"csrf_token": token}).status_code == 404  # already decided
    approved = sme.get("/sme/approved")
    assert approved.status_code == 200 and link.rsplit("/", 1)[1] in approved.text
    assert sme.get("/sme/questions/DOES-NOT-EXIST").status_code == 404


# ---------------------------------------------------------------------------
# Scheduler calendar
# ---------------------------------------------------------------------------

def test_scheduler_books_training_and_employee_sees_it(portal):
    sch = portal.login("sched.one")
    token, page = _token(sch, "/scheduler/schedule")
    assert "E-TNI|TRN-A" in page  # only real, recommended courses are offered
    day = _future(3)
    r = sch.post("/scheduler/schedule/events", data={
        "csrf_token": token, "event_type": "training", "training_choice": "E-TNI|TRN-A",
        "event_date": day.isoformat(), "start_time": "14:00", "duration": "60"})
    assert r.status_code == 303 and r.headers["location"] == f"/scheduler/schedule?month={day:%Y-%m}"
    calendar = sch.get(r.headers["location"]).text
    assert "Training booked" in calendar and 'class="cal-chip training"' in calendar
    learning = portal.login("emp.tni").get("/employee/learning").text
    assert "✓ Booked" in learning and day.isoformat() in learning


def test_scheduler_rejects_invalid_bookings(portal):
    sch = portal.login("sched.one")
    token, _ = _token(sch, "/scheduler/schedule")
    ok_day = _future(3).isoformat()
    cases = [
        {"event_type": "training", "training_choice": "E-TNI|TRN-B", "event_date": ok_day},  # not a gap course
        {"event_type": "assessment", "employee_id": "M-ONE", "event_date": ok_day},  # not an employee
        {"event_type": "assessment", "employee_id": "E-TNI", "event_date": (date.today() - timedelta(days=1)).isoformat()},
        {"event_type": "assessment", "employee_id": "E-TNI", "event_date": "31/12/2026"},
        {"event_type": "assessment", "employee_id": "E-TNI", "event_date": ok_day, "start_time": "25:99"},
    ]
    for data in cases:
        r = sch.post("/scheduler/schedule/events", data={"csrf_token": token, **data})
        assert r.status_code == 400 and "Not booked" in r.text, data
    assert sch.post("/scheduler/schedule/events", data={"csrf_token": "forged", "event_type": "assessment",
                                                         "employee_id": "E-TNI", "event_date": ok_day}).status_code == 403
    emp_token, _ = _token(portal.login("emp.tni"), "/employee/dashboard")
    assert portal.login("emp.tni").post("/scheduler/schedule/events", data={"csrf_token": emp_token}).status_code == 403


def test_auto_schedule_books_recommended_courses_without_double_booking(portal):
    sch = portal.login("sched.one")
    token, _ = _token(sch, "/scheduler/schedule")
    before = {e["course_id"] for e in portal.db.list_events("2000-01-01", "2100-12-31", user_id="E-TNI")}
    data = {"csrf_token": token, "scope": "E-TNI", "start_date": _future(1).isoformat(),
            "per_employee": "3", "cadence_days": "7", "start_time": "10:00"}
    assert sch.post("/scheduler/schedule/auto-training", data=data).status_code == 303
    assert sch.post("/scheduler/schedule/auto-training", data=data).status_code == 303  # re-run is a no-op
    events = portal.db.list_events("2000-01-01", "2100-12-31", user_id="E-TNI")
    courses = [e["course_id"] for e in events if e["course_id"]]
    # Only TRN-A is a real course for this person's gaps, and it is booked exactly once.
    assert courses.count("TRN-A") == 1 and set(courses) == before | {"TRN-A"}
    slots = [(e["event_date"], e["start_time"]) for e in events]
    assert len(slots) == len(set(slots))


def test_cancelled_booking_leaves_the_calendar(portal):
    sch = portal.login("sched.one")
    day = _future(10)
    token, _ = _token(sch, "/scheduler/schedule")
    sch.post("/scheduler/schedule/events", data={"csrf_token": token, "event_type": "assessment",
                                                 "employee_id": "E-NEW", "event_date": day.isoformat()})
    event = next(e for e in portal.db.list_events(day.isoformat(), day.isoformat(), user_id="E-NEW"))
    assert "Skill assessment" in portal.login("emp.new").get("/employee/assessments").text
    r = sch.post(f"/scheduler/schedule/events/{event['event_id']}/cancel", data={"csrf_token": token, "month": f"{day:%Y-%m}"})
    assert r.status_code == 303
    assert not portal.db.list_events(day.isoformat(), day.isoformat(), user_id="E-NEW")
    assert sch.post(f"/scheduler/schedule/events/{event['event_id']}/cancel", data={"csrf_token": token}).status_code == 404


def test_calendar_month_navigation_and_bad_month(portal):
    sch = portal.login("sched.one")
    page = sch.get("/scheduler/schedule?month=2026-02&kind=training").text
    assert "February 2026" in page and "month=2026-01" in page and "month=2026-03" in page
    assert sch.get("/scheduler/schedule?month=garbage&kind=<x>").status_code == 200


# ---------------------------------------------------------------------------
# Employee assessments tab and calendar
# ---------------------------------------------------------------------------

def _cycle(portal, user_id, status, **fields):
    import json as _json

    cycle_id = portal.db.create_cycle(
        user_id=user_id, resume_filename="cv.txt", resume_text="Uploaded: cv.txt", matched_role_id=ROLE,
        matched_role_name="Test Preparer", matched_role_grade="B3", match_status="auto_matched", status=status)
    encoded = {k: _json.dumps(v) if k.endswith("_json") else v for k, v in fields.items()}
    if encoded:
        portal.db.update_cycle(cycle_id, **encoded)
    return cycle_id


def test_assessments_tab_shows_cycles_bookings_and_tni_baseline(portal):
    _cycle(portal, "E-BASE", "pending_manager_approval")
    portal.db.create_event(event_type="assessment", title="Skill assessment · Test Preparer",
                           event_date=_future(5).isoformat(), user_id="E-BASE", duration_minutes=45)
    page = portal.login("emp.base").get("/employee/assessments").text
    assert "awaiting manager review" in page and "Scheduled for" in page
    assert page.count("data-assessment-status=") == 3  # cycle, booking, and the role-skill baseline row
    tni_page = portal.login("emp.tni").get("/employee/assessments").text
    assert "TNI baseline" in tni_page and "1 skill(s) still to be assessed" in tni_page
    assert "61.37" not in page  # scores are never shown to the employee


def test_employee_calendar_shows_only_their_own_bookings(portal):
    day = _future(12)
    portal.db.create_event(event_type="training", title="Mine", event_date=day.isoformat(), user_id="E-NEW")
    portal.db.create_event(event_type="training", title="Someone else's", event_date=day.isoformat(), user_id="E-OTHER")
    page = portal.login("emp.new").get(f"/employee/calendar?month={day:%Y-%m}").text
    assert "Mine" in page and "Someone else" not in page
    assert "/cancel" not in page  # employees can't cancel bookings


# ---------------------------------------------------------------------------
# Manager enrolment
# ---------------------------------------------------------------------------

def test_manager_enrols_completes_and_withdraws_only_for_own_team(portal):
    mgr = portal.login("mgr.one")
    token, page = _token(mgr, "/manager/training?employee=E-TNI")
    assert "Skill Alpha" in page
    today = date.today().isoformat()
    already = {e["course_id"] for e in portal.db.list_events("0000-01-01", "9999-12-31", user_id="E-TNI")}
    if "TRN-A" not in already:
        r = mgr.post("/manager/training/enroll", data={"csrf_token": token, "employee_id": "E-TNI", "course_id": "TRN-A",
                                                       "event_date": today, "start_time": "09:00"})
        assert r.status_code == 303
    r = mgr.post("/manager/training/enroll", data={"csrf_token": token, "employee_id": "E-TNI", "course_id": "TRN-A", "event_date": today})
    assert r.status_code == 400 and "already enrolled" in r.text
    r = mgr.post("/manager/training/enroll", data={"csrf_token": token, "employee_id": "E-OTHER", "course_id": "TRN-A", "event_date": today})
    assert r.status_code == 403  # not this manager's report
    assert portal.login("mgr.two").post("/manager/training/enroll-all", data={
        "csrf_token": _token(portal.login("mgr.two"), "/manager/training")[0], "employee_id": "E-TNI",
        "start_date": today}).status_code == 403

    booking = portal.db.create_event(event_type="training", title="Role-based assessment remediation for Skill Alpha",
                                     event_date=today, user_id="E-NEW", course_id="TRN-A")
    upcoming = portal.db.create_event(event_type="training", title="Later", event_date=_future(20).isoformat(),
                                      user_id="E-NEW", course_id="TRN-B")
    assert mgr.post(f"/manager/training/{booking}/complete", data={"csrf_token": token}).status_code == 303
    assert portal.db.get_event(booking)["status"] == "completed"
    assert mgr.post(f"/manager/training/{upcoming}/complete", data={"csrf_token": token}).status_code == 400  # not due yet
    assert mgr.post(f"/manager/training/{upcoming}/withdraw", data={"csrf_token": token}).status_code == 303
    assert portal.db.get_event(upcoming)["status"] == "cancelled"
    other = portal.db.create_event(event_type="training", title="x", event_date=today, user_id="E-OTHER")
    assert mgr.post(f"/manager/training/{other}/complete", data={"csrf_token": token}).status_code == 403


# ---------------------------------------------------------------------------
# Leader reporting and calibration
# ---------------------------------------------------------------------------

def test_leader_report_and_csv_export_guard_against_formula_injection(portal):
    leader = portal.login("leader.one")
    page = leader.get("/leader/reports")
    assert page.status_code == 200 and "Capability by tower" in page.text and "Top skill gaps" in page.text
    export = leader.get("/leader/reports/export.csv")
    assert export.status_code == 200 and export.headers["content-type"].startswith("text/csv")
    assert "attachment" in export.headers["content-disposition"]
    assert "'=HYPERLINK" in export.text and ",=HYPERLINK" not in export.text
    assert portal.login("mgr.one").get("/leader/reports/export.csv").status_code == 403


def test_leader_rereview_sends_the_decision_back_to_the_manager(portal):
    weak = {"evidence_results": [], "evidence_score_pct": 20, "mcq_score_pct": 30,
            "compatibility_score_pct": 25, "compatibility_band": "Not yet ready"}
    cycle_id = _cycle(portal, "E-NEW", "manager_approved", evidence_report_json=weak,
                      mcq_score_json={"overall_score_pct": 30}, manager_decision="approved", manager_id="M-ONE")
    leader = portal.login("leader.one")
    page = leader.get("/leader/calibration").text
    assert "Approved despite weak evidence" in page and "No written rationale" in page
    token, detail = _token(leader, f"/leader/calibration/{cycle_id}")
    assert "Bias signal" in detail
    r = leader.post(f"/leader/calibration/{cycle_id}/review", data={"csrf_token": token, "outcome": "rereview", "note": "no"})
    assert r.status_code == 400  # a re-review needs a reason
    r = leader.post(f"/leader/calibration/{cycle_id}/review", data={
        "csrf_token": token, "outcome": "rereview", "note": "Approved against a Not-yet-ready evidence band; please re-check."})
    assert r.status_code == 303 and portal.db.get_cycle(cycle_id)["status"] == "pending_manager_approval"
    assert leader.post(f"/leader/calibration/{cycle_id}/review",
                       data={"csrf_token": token, "outcome": "concur"}).status_code == 400  # already back with the manager

    mgr = portal.login("mgr.one")
    assert "Re-review requested by leader" in mgr.get("/manager/validations").text
    token, detail = _token(mgr, f"/manager/validations/{cycle_id}")
    assert "please re-check" in detail
    r = mgr.post(f"/manager/validations/{cycle_id}/decision", data={"csrf_token": token, "decision": "rejected", "note": "Agreed."})
    assert r.status_code == 303 and portal.db.get_cycle(cycle_id)["status"] == "manager_rejected"

    token, _ = _token(leader, f"/leader/calibration/{cycle_id}")
    assert leader.post(f"/leader/calibration/{cycle_id}/review", data={"csrf_token": token, "outcome": "concur"}).status_code == 303
    outcomes = [r["outcome"] for r in portal.db.list_calibration_reviews(cycle_id)]
    assert outcomes == ["concur", "rereview"]
    assert mgr.post(f"/leader/calibration/{cycle_id}/review", data={"csrf_token": token, "outcome": "concur"}).status_code == 403


# ---------------------------------------------------------------------------
# Question bank, SME assignment and automatic baseline
# ---------------------------------------------------------------------------

def _bank_question(portal, qid, skill, level, approve=True):
    item = {"question_id": qid, "role_id": ROLE, "role_name": "Test Preparer", "skill": skill,
            "target_proficiency_level": level, "is_critical": skill == "Skill Alpha", "question_text": f"{skill} question {qid}?",
            "option_a": "a", "option_b": "b", "option_c": "c", "option_d": "d", "correct_option": "A",
            "difficulty": "Role Ready", "ai_confidence": 0.9, "self_check": {}, "generation_attempts": 1}
    portal.db.insert_bank_questions([item], "S-ONE")
    if approve:
        assert portal.db.decide_bank_question(qid, "approved", "S-ONE")


def test_selection_agent_prefers_unassessed_skills_levels_and_fresh_questions(portal):
    from services import question_bank

    _bank_question(portal, "QB-A1", "Skill Alpha", 4)
    _bank_question(portal, "QB-A2", "Skill Alpha", 2)
    _bank_question(portal, "QB-D1", "Skill Delta", 3)
    _bank_question(portal, "QB-X", "Skill Gamma", 3, approve=False)  # pending: never selected
    sel = question_bank.select_questions(ROLE, 2, None, focus_skills=["Skill Delta"])
    assert [q["question_id"] for q in sel["selected"]] == ["QB-D1", "QB-A1"]  # unassessed skill first, then level match
    assert "unassessed skill" in sel["selected"][0]["reason"]
    assert "QB-X" not in {q["question_id"] for q in sel["selected"] + sel["others"]}
    assert "Skill Beta" in sel["uncovered"]
    assert [q["question_id"] for q in question_bank.select_questions(ROLE, 1, 2)["selected"]] == ["QB-A2"]  # SME-chosen L2


def test_baseline_is_auto_assigned_from_approved_questions_only(portal):
    from services import question_bank

    # An approved assessment for the current role counts as the evaluation; dataset levels alone don't.
    for c in portal.db.list_cycles_for_user("E-NEW"):
        portal.db.update_cycle(c["cycle_id"], status="manager_approved", manager_decision="approved")
    assert question_bank.baseline_need("E-NEW") == {"needs": False, "reason": "Evaluated for the current role"}
    page = portal.login("emp.tni").get("/employee/dashboard").text  # E-TNI: Skill Delta never assessed, no cycles
    cycle = portal.db.get_active_cycle_for_user("E-TNI")
    assert cycle and cycle["match_status"] == "auto_baseline" and cycle["status"] == "ready_for_assessment"
    assert "baseline skill assessment" in page.lower()
    questions = portal.db.get_questions_for_cycle(cycle["cycle_id"])
    assert questions and all(q["sme_status"] == "approved" for q in questions)
    assert all('"bank_question_id"' in q["item_json"] for q in questions)
    # Idempotent: a second visit doesn't assign another.
    portal.login("emp.tni").get("/employee/dashboard")
    assert len(portal.db.list_cycles_for_user("E-TNI")) == 1


def test_sme_assigns_a_bank_assessment_to_employees_and_books_a_due_date(portal):
    sme = portal.login("sme.one")
    assert "E-OTHER" in sme.get("/sme/assign").text
    token, page = _token(sme, f"/sme/assessments/new?role={ROLE}&count=2&employee=E-OTHER")
    assert 'value="E-OTHER" checked' in page
    due = _future(8)
    before = portal.db.get_bank_question("QB-D1")["times_used"]
    r = sme.post("/sme/assessments/deploy", data={"csrf_token": token, "role_id": ROLE, "question_ids": ["QB-D1", "QB-A1"],
                                                  "employee_ids": ["E-OTHER", "E-TNI"], "due_date": due.isoformat()})
    assert r.status_code == 303
    flash = sme.get("/sme/assign").text
    assert "deployed to 1 employee" in flash and "E-TNI: already has an assessment in progress" in flash
    cycle = portal.db.get_active_cycle_for_user("E-OTHER")
    assert cycle["match_status"] == "sme_assigned" and len(portal.db.get_questions_for_cycle(cycle["cycle_id"], "approved")) == 2
    assert portal.db.get_bank_question("QB-D1")["times_used"] == before + 1  # reused, not regenerated
    assert any(e["event_type"] == "assessment" for e in portal.db.list_events(due.isoformat(), due.isoformat(), user_id="E-OTHER"))
    # Bad input is refused without deploying anything.
    r = sme.post("/sme/assessments/deploy", data={"csrf_token": token, "role_id": ROLE, "question_ids": ["QB-X"], "employee_ids": ["E-BASE"]})
    assert r.status_code == 400 and "Select at least one approved question" in r.text
    # The SME can use the scheduler calendar; an employee can't use the SME tools.
    assert sme.get("/scheduler/schedule").status_code == 200
    assert portal.login("emp.base").get("/sme/assign").status_code == 403


def test_resume_upload_has_no_question_count(portal):
    # A new employee with no role on file: nothing to auto-assign, so they see the upload form.
    portal.db.create_user(username="emp.fresh", email="emp.fresh@test.local", password=PASSWORD, full_name="Emp Fresh",
                          role="employee", manager_id="M-ONE")
    page = portal.login("emp.fresh").get("/employee/dashboard").text
    assert 'name="resume"' in page and "max_items" not in page and "An SME then sets up your assessment" in page


def test_manager_sees_the_ai_use_flag_and_adjusted_score(portal):
    evidence = {"evidence_results": [{"question_id": "CS-1", "skill": "Skill Alpha", "target_proficiency_level": 4, "is_critical": True,
                                      "verdict": "strong_evidence", "quotes": [], "missing_element": None, "reasoning": "",
                                      "integrity": {"risk": "high", "penalty_factor": 0.5,
                                                    "signals": ["Response 1: 100% of the text was pasted in."]},
                                      "transcript": [{"role": "candidate", "text": "pasted answer", "meta": {"paste_chars": 13, "active_seconds": 4}}]}],
                "evidence_score_pct": 50.0, "raw_evidence_score_pct": 100.0, "integrity_risk": "high", "integrity_flagged": 1,
                "compatibility_score_pct": 60.0, "compatibility_band": "Potential fit -- development areas"}
    cycle_id = _cycle(portal, "E-BASE", "pending_manager_approval", evidence_report_json=evidence,
                      mcq_score_json={"overall_score_pct": 70})
    mgr = portal.login("mgr.one")
    assert "⚠ High" in mgr.get("/manager/validations").text
    page = mgr.get(f"/manager/validations/{cycle_id}").text
    assert "Possible AI-assisted responses" in page and "from 100.0% to 50.0%" in page
    assert "100% of the text was pasted in" in page and "100% pasted" in page


SIM_CASE = {"case_studies": [{"question_id": "CS-9", "skill": "Skill Alpha", "target_proficiency_level": 3,
                              "scenario_title": "Month-end variance", "scenario": "A variance appears on the bank rec.",
                              "question_text": "What do you do first?"}]}
SIM_STATE = {"mode": "simulation", "completed": {"CS-9": "sufficient_evidence"}, "transcripts": {"CS-9": [
    {"role": "candidate", "text": "I reconcile the ledger first."},
    {"role": "agent", "text": "Who do you tell, and with what?"},
    {"role": "candidate", "text": "My lead, with the reconciliation workbook."}]}}


def test_a_submission_reaches_the_manager_even_when_agent4_is_unavailable(portal, monkeypatch):
    from services import api_client
    from services.backend_api import BackendAPIError

    def rate_limited(*args, **kwargs):
        raise BackendAPIError("Backend returned HTTP 503", 503)

    monkeypatch.setattr(api_client, "BACKGROUND_EVIDENCE_RETRY", False)
    monkeypatch.setattr(api_client, "backend_evidence", rate_limited)
    cycle_id = _cycle(portal, "E-NEW", "mcq_submitted", mcq_score_json={"overall_score_pct": 70, "by_skill": []},
                      case_study_json=SIM_CASE, case_study_answers_json=SIM_STATE)
    assert api_client.submit_employee_case_study(cycle_id) == {"evidence_pending": True}
    cycle = portal.db.get_cycle(cycle_id)
    assert cycle["status"] == "pending_manager_approval" and cycle["evidence_report"] is None  # not stranded

    mgr = portal.login("mgr.one")
    queue = mgr.get("/manager/validations").text
    assert f"/manager/validations/{cycle_id}" in queue and "Preparing…" in queue
    token, page = _token(mgr, f"/manager/validations/{cycle_id}")
    assert "still being prepared" in page and "Generate report now" in page
    assert "Simulation conversation" in page and "Who do you tell, and with what?" in page
    assert "My lead, with the reconciliation workbook." in page

    # Deciding before the report exists is allowed, with a rationale on record.
    r = mgr.post(f"/manager/validations/{cycle_id}/decision", data={"csrf_token": token, "decision": "rejected"})
    assert r.status_code == 400 and "evidence report isn&#39;t ready" in r.text
    assert portal.db.get_cycle(cycle_id)["status"] == "pending_manager_approval"

    # The retry stores the report and drops the plan that was built without it.
    portal.db.update_cycle(cycle_id, dev_plan_json='{"status": "ok"}')
    monkeypatch.setattr(api_client, "backend_evidence", lambda *a, **k: {"evidence_results": [], "evidence_score_pct": 64.0})
    assert api_client.retry_evidence_report(cycle_id) is True
    cycle = portal.db.get_cycle(cycle_id)
    assert cycle["evidence_report"]["evidence_score_pct"] == 64.0 and cycle["dev_plan"] is None
    assert "still being prepared" not in mgr.get(f"/manager/validations/{cycle_id}").text


def test_a_late_evidence_report_never_lands_under_a_decision(portal, monkeypatch):
    from services import api_client

    cycle_id = _cycle(portal, "E-NEW", "manager_rejected", manager_decision="rejected",
                      case_study_json=SIM_CASE, case_study_answers_json=SIM_STATE)
    monkeypatch.setattr(api_client, "backend_evidence", lambda *a, **k: {"evidence_results": [], "evidence_score_pct": 99.0})
    assert api_client.retry_evidence_report(cycle_id) is False
    assert portal.db.save_pending_evidence(cycle_id, "{}") is False
    assert portal.db.get_cycle(cycle_id)["evidence_report"] is None


def test_manager_sees_assessments_in_progress_but_cannot_decide_them(portal):
    cycle_id = _cycle(portal, "E-TNI", "mcq_submitted", case_study_json=SIM_CASE, case_study_answers_json=SIM_STATE)
    mgr = portal.login("mgr.one")
    assert "Simulation finished · not submitted yet" in mgr.get("/manager/validations").text
    page = mgr.get(f"/manager/validations/{cycle_id}").text
    assert "Not submitted yet" in page and f"/manager/validations/{cycle_id}/decision" not in page
    assert "Simulation conversation" in page  # the manager can already read what was said
    assert portal.login("mgr.two").get(f"/manager/validations/{cycle_id}").status_code == 404


def test_leader_calibration_shows_the_conversation_even_for_one_shot_answers(portal):
    cycle_id = _cycle(portal, "E-BASE", "manager_approved", manager_decision="approved", manager_id="M-ONE",
                      case_study_json=SIM_CASE, case_study_answers_json={"CS-9": "I would stop the payment and escalate."},
                      evidence_report_json={"evidence_results": [{"question_id": "CS-9", "skill": "Skill Alpha",
                                                                  "verdict": "strong_evidence"}],
                                            "evidence_score_pct": 80.0})
    page = portal.login("leader.one").get(f"/leader/calibration/{cycle_id}").text
    assert "Simulation conversation" in page and "I would stop the payment and escalate." in page
    assert "Read this conversation" in page
