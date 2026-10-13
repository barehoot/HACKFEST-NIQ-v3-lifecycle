"""
Organisation design in the portal:

  - onboarding a job profile creates a new vertical and department, then Agent 8's pitch
    waits for a person: ticked skills apply with the person's edits, unticked ones are
    dismissed, and the SMEs are asked for questions; nothing half-made is left behind
    when the form is wrong
  - a manager's accepted pitch becomes a proposal; an unavailable advisor stores nothing
  - the skill catalogue: HR / admin add skills by hand (blank levels take the generic
    wording) or accept Agent 8 recommendations; nobody else can
  - HR access: the HR workspace, org-wide read of people, structure and skills, never
    performance records; an admin grants the role; a pre-HR database is migrated
  - leaders, HR and admins see people grouped by line manager first; a manager's
    reassignment moves the employee to the new team straight away
  - the calendar agenda can be minimised and the bookings export as .ics

Uses the lifecycle suite's throwaway database and stubbed pipeline API.
"""
import re
import sqlite3
from datetime import timedelta

from test_lifecycle import ADVICE, CALLS, PASSWORD, SA, TODAY, _token, portal  # noqa: F401 -- portal: the shared fixture

INDICATOR = "Builds driver-based forecast models for two business units and explains variances to finance leads."
ONBOARD = {"vertical": "__new__", "new_vertical_code": "fpa", "new_vertical_name": "Financial Planning & Analysis",
           "new_vertical_description": "Budgets and forecasts", "department": "__new__",
           "new_department_name": "Planning & Analytics", "new_department_leader": "L-ONE",
           "name": "FP&A Analyst", "process_group": "Forecasting", "grade": "b4",
           "summary": "Builds the monthly rolling forecast and variance commentary for the business units.",
           "responsibilities": "Forecasting\nVariance analysis", "skills_text": "Excel modelling, Power BI", "ai": "1"}


def _pitch(*skills, generated_by="agent"):
    ADVICE["role"] = {"generated_by": generated_by, "summary": "pitch", "skills": list(skills)}


def _suggestions(portal, role_id, statuses=("pending",)):
    return portal.store.list_skill_suggestions(scope="role", role_id=role_id, statuses=statuses)


# ---------------------------------------------------------------------------
# HR access
# ---------------------------------------------------------------------------

def test_hr_workspace_reads_org_wide_but_records_no_performance(portal):
    hr = portal.login("hr.one")
    page = hr.get("/hr/dashboard")
    assert page.status_code == 200 and "HR workspace" in page.text and "Teams that need attention" in page.text
    for path in ("/people", "/people/E-A", "/org/skills", "/org/onboard", "/org/people", "/org/job-profiles"):
        assert hr.get(path).status_code == 200, path
    tracker = hr.get("/leader/probation").text
    assert "Probation tracker" in tracker and "/decision" not in tracker  # leaders decide, HR watches
    assert hr.post("/people/E-A/evidence", data={"csrf_token": _token(hr), "kind": "concern", "title": "x" * 5,
                                                  "summary": "y" * 20}).status_code == 403
    assert hr.post("/people/E-A/insight", data={"csrf_token": _token(hr)}).status_code == 403
    assert hr.post("/people/E-A/escalations/ESC-X", data={"csrf_token": _token(hr), "status": "resolved"}).status_code == 403
    assert "Generate insight" not in hr.get("/people/E-A").text
    assert hr.get("/admin/users").status_code == 403
    assert portal.login("leader.one").get("/org/skills").status_code == 403
    assert portal.login("mgr.one").get("/org/onboard").status_code == 403
    assert portal.login("emp.a").get("/hr/dashboard").status_code == 403


def test_admin_grants_hr_access_and_a_reassignment_moves_the_team(portal):
    admin = portal.login("admin.one")
    token = _token(admin, "/admin/users")
    r = admin.post("/admin/users", data={"csrf_token": token, "username": "hr.two", "email": "hr.two@test.local",
                                         "password": PASSWORD, "full_name": "Hr Two", "role": "hr"})
    assert r.status_code == 303
    assert portal.login("hr.two").get("/hr/dashboard").status_code == 200

    assert admin.post("/admin/users/E-B/role", data={"csrf_token": token, "role": "employee", "manager_id": "NOPE"}).status_code == 400
    assert admin.post("/admin/users/E-B/role", data={"csrf_token": token, "role": "employee", "manager_id": "M-TWO"}).status_code == 303
    assert portal.store.status_rows(["E-B"])["E-B"]["manager_id"] == "M-TWO"  # snapshot follows at once
    leader = portal.login("leader.one")
    assert "Emp B" in leader.get("/people?manager=M-TWO").text
    admin.post("/admin/users/E-B/role", data={"csrf_token": token, "role": "employee", "manager_id": "M-ONE"})
    assert "Emp B" not in leader.get("/people?manager=M-TWO").text


def test_pre_hr_database_is_migrated_in_place(portal, tmp_path):
    db_file = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE users (user_id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, email TEXT UNIQUE NOT NULL, "
                 "password_hash TEXT NOT NULL, full_name TEXT, role TEXT NOT NULL CHECK(role IN "
                 "('employee','sme','scheduler','manager','leader','admin')), department TEXT, job_title TEXT, "
                 "manager_id TEXT REFERENCES users(user_id), created_at TEXT)")
    conn.execute("CREATE INDEX idx_users_role ON users(role)")
    conn.execute("CREATE TABLE notes (id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(user_id))")
    conn.execute("INSERT INTO users VALUES ('M', 'm', 'm@x', 'h', 'M', 'manager', '', '', NULL, NULL)")
    conn.execute("INSERT INTO users VALUES ('E', 'e', 'e@x', 'h', 'E', 'employee', '', '', 'M', NULL)")
    conn.execute("INSERT INTO notes VALUES ('N', 'E')")
    conn.commit()
    conn.close()
    saved = portal.db.DB_PATH
    portal.db.DB_PATH = db_file
    try:
        portal.db.ensure_user_roles()
        portal.db.ensure_user_roles()  # idempotent
    finally:
        portal.db.DB_PATH = saved
    conn = sqlite3.connect(db_file)
    conn.execute("PRAGMA foreign_keys = ON")
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
    conn.execute("INSERT INTO users VALUES ('H', 'h', 'h@x', 'h', 'H', 'hr', '', '', NULL, NULL)")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'idx_users_role'").fetchone()
    conn.close()


# ---------------------------------------------------------------------------
# Onboarding a job profile
# ---------------------------------------------------------------------------

def test_onboarding_creates_vertical_department_role_and_reviews_the_ai_pitch(portal):
    _pitch({"skill": "skill a", "existing": True, "capability": "Tech", "target_level": 4, "is_critical": True,
            "behavior_indicator": "", "rationale": "Core of the job"},
           {"skill": "Driver-Based Forecasting", "existing": False, "capability": "Analytics", "target_level": 4,
            "is_critical": False, "behavior_indicator": INDICATOR, "rationale": "Forecasting is the job"},
           {"skill": "driver-based forecasting", "existing": False, "target_level": 3, "behavior_indicator": INDICATOR},
           {"skill": "x", "target_level": 3})
    hr = portal.login("hr.one")
    r = hr.post("/org/onboard", data={**ONBOARD, "csrf_token": _token(hr, "/org/onboard")})
    assert r.status_code == 303 and r.headers["location"] == "/org/onboard/FPA_fp_a_analyst"
    role_id = "FPA_fp_a_analyst"
    assert "FPA" in portal.job_profiles.towers()
    dept = next(d for d in portal.store.list_departments() if d["name"] == "Planning & Analytics")
    assert dept["tower"] == "FPA" and dept["leader_id"] == "L-ONE"
    detail = portal.job_profiles.role_detail(role_id)
    assert detail["role"]["role_grade"] == "B4" and detail["profile"]["dept_id"] == dept["dept_id"]
    sent = CALLS["advice"][-1]
    assert sent["mode"] == "role" and sent["role_name"] == "FP&A Analyst" and sent["tower"] == "FPA"
    assert "Skill A" in {c["skill"] for c in sent["catalogue"]}

    pending = _suggestions(portal, role_id)
    assert [s["skill"] for s in pending] == ["Skill A", "Driver-Based Forecasting"]  # canonical, de-duplicated, cleaned
    skill_a = next(s for s in pending if s["skill"] == "Skill A")
    assert skill_a["payload"]["existing"] is True and skill_a["payload"]["behavior_indicator"] == "a4"  # from the catalogue
    page = hr.get(r.headers["location"]).text
    assert "Driver-Based Forecasting" in page and "AI-generated" in page and "Readiness" in page

    token = _token(hr, r.headers["location"])
    sid = skill_a["suggestion_id"]
    # The catalogue's two-letter indicator is too vague for a new requirement: the person must improve it.
    r = hr.post(f"/org/job-profiles/{role_id}/suggestions", data={"csrf_token": token, "back": "onboard", f"accept_{sid}": "1",
                                                                   f"level_{sid}": "3"})
    assert r.status_code == 303 and portal.job_profiles.role_detail(role_id)["skills"] == []
    assert len(_suggestions(portal, role_id)) == 1  # the unticked one was dismissed; Skill A still waits

    r = hr.post(f"/org/job-profiles/{role_id}/suggestions", data={
        "csrf_token": token, "back": "onboard", f"accept_{sid}": "1", f"level_{sid}": "3", f"critical_{sid}": "1",
        f"behavior_{sid}": "Resolves complex invoice exceptions independently and documents the root causes."})
    assert r.status_code == 303
    skills = portal.job_profiles.role_detail(role_id)["skills"]
    assert [(s["skill"], s["target_level"], s["is_critical"]) for s in skills] == [("Skill A", 3, 1)]
    decided = {s["skill"]: s["status"] for s in _suggestions(portal, role_id, ("accepted", "dismissed"))}
    assert decided == {"Skill A": "accepted", "Driver-Based Forecasting": "dismissed"}
    change = portal.store.list_skill_changes(role_id=role_id)[0]
    assert change["status"] == "applied" and "Agent 8" in change["reason"]
    assert any(n["link"] == f"/sme/generate?role={role_id}" for n in portal.store.list_notifications("S-ONE"))
    ready = {c["key"]: c["ok"] for c in portal.job_profiles.readiness(role_id)["checks"]}
    assert ready["department"] and ready["critical"] and not ready["skills"] and not ready["questions"]


def test_onboarding_validates_before_creating_anything(portal):
    hr = portal.login("hr.one")
    token = _token(hr, "/org/onboard")
    before = set(portal.job_profiles.towers())
    r = hr.post("/org/onboard", data={**ONBOARD, "new_vertical_code": "TAX", "new_vertical_name": "Tax", "grade": "Senior",
                                      "csrf_token": token})
    assert r.status_code == 400 and "Grade" in r.text and set(portal.job_profiles.towers()) == before
    r = hr.post("/org/onboard", data={**ONBOARD, "new_vertical_code": "AP", "new_vertical_name": "Another AP", "csrf_token": token})
    assert r.status_code == 400 and "already exists" in r.text
    assert not any(d["name"] == "Planning & Analytics 2" for d in portal.store.list_departments())
    r = hr.post("/org/onboard", data={**ONBOARD, "vertical": "AP", "department": "", "name": "P2P Senior Associate", "csrf_token": token})
    assert r.status_code == 400 and "already exists in this vertical" in r.text
    r = hr.post("/org/onboard", data={**ONBOARD, "vertical": "R2R", "department": "__new__", "new_department_name": "",
                                      "csrf_token": token})
    assert r.status_code == 400 and "Name the new department" in r.text
    # Leaders onboard too; without the AI box ticked, Agent 8 isn't called.
    leader = portal.login("leader.one")
    calls = len(CALLS["advice"])
    r = leader.post("/org/onboard", data={**ONBOARD, "vertical": "AP", "department": "", "name": "AP Automation Analyst",
                                          "ai": "", "csrf_token": _token(leader, "/org/onboard")})
    assert r.status_code == 303 and len(CALLS["advice"]) == calls
    assert portal.login("mgr.one").post("/org/onboard", data={**ONBOARD, "csrf_token": token}).status_code == 403


def test_manager_pitch_becomes_proposals_and_an_unavailable_advisor_stores_nothing(portal):
    _pitch({"skill": "Vendor Master Data", "existing": False, "capability": "Controls", "target_level": 3, "is_critical": False,
            "behavior_indicator": "Maintains vendor master records with dual control and monthly duplicate checks.",
            "rationale": "Duplicate payments are rising"},
           {"skill": "Skill B", "existing": True, "target_level": 3, "behavior_indicator": "applies controls well every day"})
    mgr = portal.login("mgr.one")
    token = _token(mgr, f"/org/job-profiles/{SA}")
    r = mgr.post(f"/org/job-profiles/{SA}/suggest", data={"csrf_token": token, "back": "detail"})
    assert r.status_code == 303 and r.headers["location"] == f"/org/job-profiles/{SA}#ai-skills"
    pending = _suggestions(portal, SA)
    assert [s["skill"] for s in pending] == ["Vendor Master Data"]  # Skill B is already required
    before = len(portal.job_profiles.role_detail(SA)["skills"])
    sid = pending[0]["suggestion_id"]
    assert mgr.post(f"/org/job-profiles/{SA}/suggestions", data={"csrf_token": token, "back": "detail", f"accept_{sid}": "1",
                                                                  f"level_{sid}": "3"}).status_code == 303
    assert len(portal.job_profiles.role_detail(SA)["skills"]) == before  # a proposal, not a change
    proposal = next(c for c in portal.store.list_skill_changes(role_id=SA) if c["skill"] == "Vendor Master Data")
    assert proposal["status"] == "proposed"
    assert _suggestions(portal, SA, ("proposed",))[0]["skill"] == "Vendor Master Data"

    ADVICE["unavailable"] = True
    try:
        r = mgr.post(f"/org/job-profiles/{SA}/suggest", data={"csrf_token": token, "back": "detail"})
        assert r.status_code == 303 and "unavailable" in mgr.get(r.headers["location"]).text
        assert _suggestions(portal, SA) == []
    finally:
        ADVICE["unavailable"] = False
    _pitch({"skill": "Payment Fraud Awareness", "existing": False, "target_level": 3, "is_critical": False,
            "behavior_indicator": "Spots and escalates suspicious bank-detail change requests before payment runs."},
           generated_by="rules")
    mgr.post(f"/org/job-profiles/{SA}/suggest", data={"csrf_token": token, "back": "detail"})
    page = mgr.get(f"/org/job-profiles/{SA}").text
    assert "Payment Fraud Awareness" in page and "keyword matching" in page


# ---------------------------------------------------------------------------
# Skill catalogue
# ---------------------------------------------------------------------------

def test_skill_catalogue_by_hand_and_from_agent8(portal):
    hr = portal.login("hr.one")
    token = _token(hr, "/org/skills")
    r = hr.post("/org/skills", data={"csrf_token": token, "skill": "Process Mining", "tower": "AP", "capability": "Analytics",
                                     "l3": "Runs process-mining analyses on P2P event logs and explains the bottlenecks."})
    assert r.status_code == 303
    skill = portal.store.skill_by_name("process mining")
    assert skill["l3_indicator"].startswith("Runs process-mining") and skill["l1_indicator"] == "generic level 1 behaviour"
    assert hr.post("/org/skills", data={"csrf_token": token, "skill": "process mining", "tower": "AP"}).status_code == 400
    assert portal.login("mgr.one").post("/org/skills", data={"csrf_token": token, "skill": "Nope"}).status_code == 403

    ADVICE["catalogue"] = {"generated_by": "agent", "summary": "s", "skills": [
        {"skill": "E-Invoicing Compliance", "capability": "Controls & Compliance", "direction": "rising", "rationale": "Mandates",
         "indicators": [f"L{n}: e-invoicing behaviour at this level" for n in range(1, 6)]},
        {"skill": "Process mining", "direction": "emerging", "indicators": []},  # already in the catalogue
        {"skill": "Continuous Controls Monitoring", "direction": "emerging", "indicators": ["too few"]}]}
    r = hr.post("/org/skills/suggest", data={"csrf_token": token, "tower": "AP", "focus": "e-invoicing mandates"})
    assert r.status_code == 303 and CALLS["advice"][-1]["mode"] == "catalogue" and CALLS["advice"][-1]["focus"] == "e-invoicing mandates"
    pending = portal.store.list_skill_suggestions(scope="catalogue")
    assert [s["skill"] for s in pending] == ["E-Invoicing Compliance", "Continuous Controls Monitoring"]
    first, second = pending
    assert hr.post(f"/org/skills/suggestions/{first['suggestion_id']}/accept",
                   data={"csrf_token": token, "skill": "E-Invoicing & Tax Compliance", "capability": ""}).status_code == 303
    added = portal.store.skill_by_name("E-Invoicing & Tax Compliance")
    assert added["tower"] == "AP" and added["l2_indicator"].startswith("L2: e-invoicing")
    assert hr.post(f"/org/skills/suggestions/{second['suggestion_id']}/dismiss", data={"csrf_token": token}).status_code == 303
    assert portal.store.list_skill_suggestions(scope="catalogue") == []
    assert hr.post(f"/org/skills/suggestions/{first['suggestion_id']}/accept", data={"csrf_token": token}).status_code == 400
    admin = portal.login("admin.one")
    assert admin.get("/admin/skills").headers["location"] == "/org/skills"
    assert "E-Invoicing &amp; Tax Compliance" in admin.get("/org/skills").text


# ---------------------------------------------------------------------------
# People grouped by manager
# ---------------------------------------------------------------------------

def test_people_grouped_by_manager_with_unassigned_team(portal):
    # HR sees the whole organisation (leader.one now leads only the new, empty department).
    leader = portal.login("hr.one")
    leader.get("/people")  # fills any missing snapshot rows
    clusters, total = portal.store.manager_clusters({})
    teams = {c["manager_id"]: c["team"] for c in clusters}
    assert teams["M-ONE"] >= 3 and teams["M-TWO"] == 1 and total == len(teams)
    portal.db.update_user_role("E-C", "employee", None)
    portal.performance.refresh(["E-C"])
    try:
        page = leader.get("/people").text
        assert "No manager assigned" in page and "Assign a manager" in page
        page = leader.get("/people?manager=none").text
        assert "Emp C" in page and "Emp A" not in page and "All managers" in page
    finally:
        portal.db.update_user_role("E-C", "employee", "M-ONE")
        portal.performance.refresh(["E-C"])
    assert leader.get("/people?manager=NOPE").status_code == 404
    assert "Emp A" in leader.get("/people?q=Emp").text  # a search lists people, not teams


# ---------------------------------------------------------------------------
# Calendar: collapsible agenda and .ics export
# ---------------------------------------------------------------------------

def test_calendar_agenda_collapses_and_exports_ics(portal):
    day = TODAY + timedelta(days=3)
    portal.db.create_event(event_type="training", title="Skill A clinic, part 1; exceptions", event_date=day.isoformat(),
                           start_time="10:00", duration_minutes=45, user_id="E-A", notes="Bring two live cases\nand the SOP",
                           created_by="M-ONE")
    portal.db.create_event(event_type="assessment", title="Baseline assessment", event_date=day.isoformat(), user_id="E-A",
                           created_by="M-ONE")
    emp = portal.login("emp.a")
    page = emp.get(f"/employee/calendar?month={day.strftime('%Y-%m')}").text
    assert 'data-agenda-panel="/employee/calendar"' in page and 'class="agenda-day"' in page
    assert "Minimise" in page and "Expand agenda" in page and "/employee/calendar.ics" in page
    r = emp.get("/employee/calendar.ics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    assert "attachment" in r.headers["content-disposition"]
    text = r.text
    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n")
    assert f"DTSTART:{day.strftime('%Y%m%d')}T100000" in text and "DURATION:PT45M" in text
    assert "SUMMARY:Training: Skill A clinic\\, part 1\\; exceptions" in text
    assert "Bring two live cases\\nand the SOP" in text.replace("\r\n ", "")  # unfolded
    assert f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}" in text  # no start time -> all-day
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    assert portal.login("mgr.one").get("/employee/calendar.ics").status_code == 403
    assert re.search(r"UID:EVT-[0-9A-F]+@talent360i", text)
