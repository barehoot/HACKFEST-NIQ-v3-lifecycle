"""
Employee-lifecycle tests (v3): status engine, evidence, reviews, development plan,
escalations, probation, job-profile governance, resume-triggered coding tasks,
Outlook / Teams ingestion, engagement, notifications and access scope.

  - the status engine: only factors with data count; a 1-2/5 review caps the band;
    evidence the employee can't see never moves the score; wellbeing never does
  - a manager logs evidence; a tagged email is redacted, classified by Agent 7 and
    waits for confirmation; the employee sees only what was shared and can reply
  - reviews: sharing needs a rating and summary; acknowledging locks the review
  - development plan: Agent 6 actions are re-verified (types, the role's own skills,
    due dates) and merged with the generated plan without duplicates
  - escalation to the manager, then the leader, and automatic resolution
  - probation: hire date -> tracking -> check-ins -> recommendation -> leader decision
  - job profiles: managers propose, leaders/admins apply; TNI follows the live role
  - coding tasks trigger from resume keywords, gate the simulation submit, are reviewed
    (never run) and scored by an SME
  - every route checks who may see whom

Throwaway SQLite DB with a hand-built reference dataset; the pipeline API is stubbed.
"""
import importlib.util
import json
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
PASSWORD = "Lifecycle-Test-Pass-42!"
CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')
TODAY = date.today()

SA, LEAD, TE = "AP_sa", "AP_lead", "AP_te"
REFERENCE = {
    "role_master": [(SA, "AP", "P2P", "P2P Senior Associate", "B3", "B3"), (LEAD, "AP", "P2P", "P2P Lead", "B4", "B4"),
                    (TE, "AP", "T&E", "T&E Lead", "B4", "B4")],
    "skill_master": [("SK_A", "AP", "Skill A", "Tech", "a1", "a2", "a3", "a4", "a5"),
                     ("SK_B", "AP", "Skill B", "Tech", "b1", "b2", "b3", "b4", "b5"),
                     ("SK_C", "AP", "Skill C", "Tech", "c1", "c2", "c3", "c4", "c5")],
    "proficiency_levels": [(n, f"L{n}", f"generic level {n} behaviour", "") for n in range(1, 6)],
    "role_skill_map": [("R1", SA, "SK_A", "Skill A", "Tech", 3, "B", "handles exceptions", 1, "question", 0),
                       ("R2", SA, "SK_B", "Skill B", "Tech", 3, "B", "applies controls", 0, "question", 0),
                       ("R3", LEAD, "SK_A", "Skill A", "Tech", 4, "B", "leads exceptions", 1, "question", 0),
                       ("R6", TE, "SK_C", "Skill C", "Tech", 3, "B", "manages stakeholders", 0, "question", 0)],
    "training_catalogue": [("TRN-A", "AP", "Skill A exception clinic", "Lab", "B3", "High", "SDL")],
    "training_skill_map": [("TSM-1", "TRN-A", "AP", "SK_A", "Skill A", "Tech", "B3", "High")],
    "skill_gaps": [("G1", "E-A", SA, "T", "SK_A", "Skill A", "Tech", 3, 2.0, "1", "Moderate", "TRN-A", None, None),
                   ("G2", "E-A", SA, "T", "SK_B", "Skill B", "Tech", 3, 2.0, "1", "Moderate", None, None, None)],
}
USERS = [  # user_id, username, role, manager_id, job_title
    ("M-ONE", "mgr.one", "manager", None, None), ("M-TWO", "mgr.two", "manager", None, None),
    ("L-ONE", "leader.one", "leader", None, None), ("L-TWO", "leader.two", "leader", None, None),
    ("A-ONE", "admin.one", "admin", None, None), ("S-ONE", "sme.one", "sme", None, None),
    ("H-ONE", "hr.one", "hr", None, None),
    ("E-A", "emp.a", "employee", "M-ONE", "P2P Senior Associate"),
    ("E-B", "emp.b", "employee", "M-ONE", "P2P Senior Associate"),
    ("E-C", "emp.c", "employee", "M-ONE", "P2P Senior Associate"),
    ("E-NEW", "emp.new", "employee", "M-ONE", "P2P Senior Associate"),
    ("E-OTHER", "emp.other", "employee", "M-TWO", "P2P Senior Associate"),
]
CLASSIFIED = {"kind": "appreciation", "sentiment": "positive", "summary": "The employee cleared a vendor statement quickly.",
              "skills": ["Skill A", "Telepathy"], "action_items": ["share the reconciliation template"],
              "confidence": 0.9, "generated_by": "agent"}
AI_PLAN = {"generated_by": "agent", "summary": "s", "development_actions": [
    {"title": "AI supervised practice on exceptions", "action_type": "practice", "skill": "Skill A", "due_in_days": 30,
     "success_measure": "Zero exceptions past SLA"},
    {"title": "Invented stretch", "action_type": "stretch", "skill": "Telepathy", "due_in_days": 20},
    {"title": "Bad type", "action_type": "teleport", "skill": None, "due_in_days": 20},
    {"title": "Far future", "action_type": "reading", "skill": None, "due_in_days": 400}]}
CODE_REVIEW = {"generated_by": "agent", "flags": [], "scores": {"correctness": 4, "approach": 4, "code_quality": 3, "edge_cases": 3},
               "average": 3.5, "verdict": "meets", "summary": "Sound approach.", "strengths": [], "issues": [], "needs_sme": False}
EML = (b'From: "Priya Shah" <priya.shah@vendor.example>\r\nTo: mgr.one@test.local\r\n'
       b"Subject: Great job on the vendor reconciliation\r\nDate: Mon, 05 Oct 2026 10:00:00 +0000\r\n"
       b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
       b"Hi, many thanks for clearing the vendor statement so quickly. Call me on +44 20 7946 0958 or "
       b"write to priya.shah@vendor.example. Account 123456789012.\r\n- Next step: share the reconciliation template\r\n")
CALLS = {"insight": [], "classify": [], "review": [], "advice": [], "summary": [], "resume": [], "classify_source": []}
# Agent 9 / CV-reader stub responses (tests/test_career_mobility.py edits these per test).
SUMMARY = {"generated_by": "agent", "headline": "A steady period.", "overall": "Solid delivery with one escalation.",
           "highlights": [{"text": "Cleared the vendor statement", "refs": ["E1", "E99"]}], "concerns": [],
           "evidence_mix": "Manager notes and a tagged email.", "skills_growth": ["Skill A up a level"], "learning": [],
           "development_focus": ["Skill B"], "talking_points": [], "suggested_rating": 3, "promotion_view": None}
RESUME = {"generated_by": "agent", "claims": []}
# Agent 8 stub responses (tests/test_org_design.py edits these per test).
ADVICE = {"unavailable": False, "role": {"generated_by": "agent", "summary": "", "skills": []},
          "catalogue": {"generated_by": "agent", "summary": "", "skills": []}}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def portal(tmp_path_factory):
    import os

    from fastapi.testclient import TestClient

    tmp = tmp_path_factory.mktemp("lifecycle")
    saved_env = {k: os.environ.get(k) for k in [*_ENV, "TALENT360_LOG_DIR", "TALENT360_INTEGRATION_TOKEN"]}
    os.environ.update({**_ENV, "TALENT360_DEMO_PASSWORD": PASSWORD, "TALENT360_SYNTHETIC_PASSWORD": PASSWORD,
                       "TALENT360_LOG_DIR": str(tmp / "logs")})
    os.environ.pop("TALENT360_INTEGRATION_TOKEN", None)
    _purge_portal_modules()
    sys.path.insert(0, str(PORTAL))
    try:
        seed = _load("seed_users_for_lifecycle_test", ROOT / "scripts" / "seed_users.py")
        reference = _load("build_sqlite_for_lifecycle_test", ROOT / "scripts" / "build_sqlite.py")
        db_file = tmp / "talent360i.sqlite"
        conn = sqlite3.connect(db_file)
        for statement in reference.SCHEMA_STATEMENTS + seed.SCHEMA_STATEMENTS:
            conn.execute(statement)
        for table, rows in REFERENCE.items():
            marks = ",".join("?" * len(rows[0]))
            conn.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)  # nosec B608 -- fixed test table names
        pw_hash = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
        for user_id, username, role, manager_id, title in USERS:
            conn.execute("INSERT INTO users (user_id, username, email, password_hash, full_name, role, job_title, manager_id) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (user_id, username, f"{username}@test.local", pw_hash, username.replace(".", " ").title(), role,
                          title, manager_id))
        conn.commit()
        conn.close()

        app_module = _load("portal_app", PORTAL / "app.py")
        from services import (api_client, coding, db, development, job_profiles, lifecycle_store, mobility, performance,
                              performance_summary, probation, schedule, skill_advisor, smartrecruiters, tni)

        db.DB_PATH = db_file
        api_client.backend_classify_evidence = lambda text, skills, source: (
            CALLS["classify"].append(text), CALLS["classify_source"].append(source)) and dict(CLASSIFIED)
        api_client.backend_performance_summary = lambda payload: CALLS["summary"].append(payload) or json.loads(json.dumps(SUMMARY))
        api_client.backend_resume_skills = lambda data, name, skills: CALLS["resume"].append((data, name, skills)) or json.loads(
            json.dumps(RESUME))
        api_client.backend_performance_insight = lambda payload, mode="insight": CALLS["insight"].append((mode, payload)) or (
            dict(AI_PLAN) if mode == "plan" else {"generated_by": "agent", "summary": "Insight summary", "strengths": ["s"],
                                                  "risks": ["r"], "manager_actions": ["m"], "development_actions": [],
                                                  "escalation": {"recommended": False, "reason": ""},
                                                  "review_draft": {"summary": "Draft summary from evidence", "strengths": "x",
                                                                   "improvements": "y", "suggested_rating": 3}})
        api_client.backend_code_review = lambda challenge, code, language: CALLS["review"].append(code) or dict(CODE_REVIEW)
        from services.backend_api import BackendAPIError

        def advice(payload):
            CALLS["advice"].append(payload)
            if ADVICE["unavailable"]:
                raise BackendAPIError("Backend connection failed")
            return json.loads(json.dumps(ADVICE[payload["mode"]]))
        api_client.backend_skill_advice = advice
        api_client.backend_match_resume = lambda *a, **k: {
            "status": "auto_matched", "keyword_hits": ["python", "cobol"],
            "match": {"role_id": SA, "role_name": "P2P Senior Associate", "role_grade": "B3"}}
        api_client.backend_case_studies = lambda *a, **k: {"case_studies": [
            {"question_id": "CS-1", "skill": "Skill A", "question_text": "Handle an exception.", "target_proficiency_level": 3}]}

        def login(username):
            client = TestClient(app_module.app, follow_redirects=False)
            token = CSRF.search(client.get("/login").text).group(1)
            r = client.post("/login", data={"username": username, "password": PASSWORD, "csrf_token": token})
            assert r.status_code == 303 and r.headers["location"] != "/login", username
            return client

        yield type("Portal", (), {"login": staticmethod(login), "db": db, "store": lifecycle_store, "performance": performance,
                                  "probation": probation, "development": development, "coding": coding, "tni": tni,
                                  "api_client": api_client, "job_profiles": job_profiles, "schedule": schedule,
                                  "skill_advisor": skill_advisor, "db_file": db_file, "mobility": mobility,
                                  "performance_summary": performance_summary, "smartrecruiters": smartrecruiters})
    finally:
        sys.path.remove(str(PORTAL))
        _purge_portal_modules()
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _token(client, path="/notifications"):
    page = client.get(path)
    assert page.status_code == 200, (path, page.status_code)
    return CSRF.search(page.text).group(1)


# ---------------------------------------------------------------------------
# Status engine (pure)
# ---------------------------------------------------------------------------

def _ctx(**over):
    ctx = {"user": {"user_id": "X", "full_name": "X Y", "manager_id": "M-ONE"}, "plan": None, "role": None,
           "readiness": None, "cycle": None, "active_cycle": None, "evidence": [], "review": None, "actions": [],
           "probation": None, "pulses": [], "escalations": [], "profile": None, "previous": None}
    ctx.update(over)
    return ctx


def _plan(current, target=3, critical=False):
    skills = [{"skill": f"S{i}", "current": c, "target": target, "gap": max(target - c, 0), "assessed": True,
               "is_critical": critical, "severity": "High"} for i, c in enumerate(current)]
    at_target = sum(1 for s in skills if not s["gap"])
    return {"skills": skills, "path": [], "summary": {"assessed": len(skills), "at_target": at_target,
                                                     "readiness_pct": round(100 * at_target / len(skills))}}


def test_status_engine_counts_only_factors_with_data(portal):
    perf = portal.performance
    only_skills = perf.compute(_ctx(plan=_plan([3, 3])), TODAY)
    assert only_skills["band"] == "Not enough data" and only_skills["score"] is None
    assert "Latest performance review" in only_skills["missing"]
    rated = perf.compute(_ctx(plan=_plan([2, 2]), review={"rating": 4, "period": "2026-H1"}), TODAY)
    skills = next(f for f in rated["factors"] if f["key"] == "skills")
    assert skills["score"] == 67  # L2 of L3 = 67% attainment, not "0% at target"
    assert rated["score"] == round((67 * 30 + 80 * 25) / 55) and rated["band"] == "On track"


def test_low_review_caps_band_and_hidden_evidence_never_counts(portal):
    perf = portal.performance
    strong_skills = _plan([3, 3])
    assert perf.compute(_ctx(plan=strong_skills, review={"rating": 2, "period": "p"}), TODAY)["band"] == "At risk"
    assert perf.compute(_ctx(plan=strong_skills, review={"rating": 1, "period": "p"}), TODAY)["band"] == "Off track"
    private = [{"kind": "concern", "sentiment": "negative", "weight": 3, "shared": 0, "occurred_on": TODAY.isoformat(), "title": "t"}]
    result = perf.compute(_ctx(plan=strong_skills, evidence=private), TODAY)
    assert not any(f["key"] == "evidence" for f in result["factors"])


def test_wellbeing_is_a_signal_never_a_score(portal):
    perf = portal.performance
    ctx = _ctx(plan=_plan([3, 2]), review={"rating": 4, "period": "p"})
    low = [{"week": "w", "mood": 1, "workload": 5}] * 3
    calm, stressed = perf.compute(ctx, TODAY), perf.compute({**ctx, "pulses": low}, TODAY)
    assert calm["score"] == stressed["score"] and calm["band"] == stressed["band"]
    assert "wellbeing" in {s["key"] for s in stressed["signals"]}
    hidden = perf.for_viewer(stressed, "leader")
    assert "wellbeing" not in {s["key"] for s in hidden["signals"]}
    assert not any(r.get("signal") == "wellbeing" for r in hidden["recommendations"])


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

def test_manager_logs_evidence_and_employee_sees_only_what_is_shared(portal):
    mgr = portal.login("mgr.one")
    token = _token(mgr, "/people/E-A")
    for title, shared in (("Shared appreciation for month-end", "1"), ("Private note about tone", "")):
        r = mgr.post("/people/E-A/evidence", data={"kind": "appreciation" if shared else "concern", "title": title,
                                                    "summary": "Facts: what happened, when, and the impact on the close.",
                                                    "occurred_on": TODAY.isoformat(), "skills": ["Skill A"], "weight": "2",
                                                    "shared": shared, "csrf_token": token})
        assert r.status_code == 303
    emp = portal.login("emp.a")
    page = emp.get("/employee/performance").text
    assert "Shared appreciation for month-end" in page and "Private note about tone" not in page
    assert "Private note about tone" in mgr.get("/people/E-A").text  # the manager keeps their note
    status = portal.performance.status_for("E-A")
    assert next(f for f in status["factors"] if f["key"] == "evidence")["detail"].startswith("1 positive, 0 negative")


def test_tagged_email_is_redacted_classified_and_waits_for_confirmation(portal):
    mgr = portal.login("mgr.one")
    token = _token(mgr, "/people/E-A")
    r = mgr.post("/people/E-A/evidence/import", data={"csrf_token": token, "source": "outlook"},
                 files={"message_file": ("thanks.eml", EML, "message/rfc822")})
    assert r.status_code == 303
    assert "priya.shah@vendor.example" not in CALLS["classify"][-1] and "[phone]" in CALLS["classify"][-1]
    pending = portal.store.list_evidence("E-A", statuses=("pending",))
    item = next(e for e in pending if e["title"] == "Great job on the vendor reconciliation")
    assert item["origin_sender"] == "Priya Shah" and "[number]" in item["excerpt"] and "@" not in item["excerpt"]
    assert item["skills"] == ["Skill A"]  # "Telepathy" is not a role skill
    assert "Great job on the vendor reconciliation" not in portal.login("emp.a").get("/employee/performance").text
    again = mgr.post("/people/E-A/evidence/import", data={"csrf_token": token, "source": "outlook"},
                     files={"message_file": ("thanks.eml", EML, "message/rfc822")})
    assert again.status_code == 400 and "already tagged" in again.text
    bad = mgr.post("/people/E-A/evidence/import", data={"csrf_token": token},
                   files={"message_file": ("run.exe", b"MZ\x00\x00", "application/octet-stream")})
    assert bad.status_code == 400
    r = mgr.post(f"/people/E-A/evidence/{item['evidence_id']}/confirm",
                 data={"csrf_token": token, "kind": "appreciation", "sentiment": "positive", "title": item["title"],
                       "summary": "The employee cleared the vendor statement in a day.", "skills": ["Skill A"],
                       "shared": "1", "weight": "1", "create_actions": "1"})
    assert r.status_code == 303
    assert any(a["title"] == "share the reconciliation template" and a["source"] == "one_on_one"
               for a in portal.store.list_actions("E-A"))
    assert "Great job on the vendor reconciliation" in portal.login("emp.a").get("/employee/performance").text


def test_employee_can_reply_to_shared_evidence(portal):
    emp = portal.login("emp.a")
    item = next(e for e in portal.store.list_evidence("E-A", shared_only=True))
    r = emp.post(f"/employee/evidence/{item['evidence_id']}/comment", data={"csrf_token": _token(emp), "comment": "Context: the vendor sent the file late."})
    assert r.status_code == 303 and portal.store.get_evidence(item["evidence_id"])["employee_comment"].startswith("Context")
    other = next(e for e in portal.store.list_evidence("E-A", shared_only=False) if not e["shared"])
    assert emp.post(f"/employee/evidence/{other['evidence_id']}/comment", data={"csrf_token": _token(emp), "comment": "hmm"}).status_code == 404


def test_people_records_are_scoped(portal):
    other = portal.login("mgr.two")
    assert other.get("/people/E-A").status_code == 404
    assert other.post("/people/E-A/evidence", data={"csrf_token": _token(other), "kind": "concern", "title": "x" * 5,
                                                     "summary": "y" * 20}).status_code == 404
    assert portal.login("emp.a").get("/people/E-B").status_code == 403
    page = other.get("/people").text
    assert "Emp Other" in page and "Emp A" not in page
    leader = portal.login("leader.one")
    page = leader.get("/people").text  # leaders start on manager clusters, not people
    assert "Mgr One" in page and "Mgr Two" in page and "Emp A" not in page and "Emp Other" not in page
    assert "/people?manager=M-ONE" in page.replace("&amp;", "&")
    page = leader.get("/people?manager=M-ONE").text
    assert "Emp A" in page and "Emp Other" not in page
    page = leader.get("/people?manager=M-TWO").text
    assert "Emp Other" in page and "Emp A" not in page
    page = leader.get("/people?view=people").text
    assert "Emp Other" in page and "Emp A" in page
    assert other.get("/people?manager=M-ONE").status_code == 200 and "Emp A" not in other.get("/people?manager=M-ONE").text
    assert leader.post("/people/E-A/evidence", data={"csrf_token": _token(leader), "kind": "concern", "title": "x" * 5,
                                                      "summary": "y" * 20}).status_code == 403  # leaders read, managers record


def test_department_scoped_leader(portal):
    admin = portal.login("admin.one")
    leader = portal.login("leader.two")
    r = admin.post("/org/departments", data={"csrf_token": _token(admin, "/org/job-profiles"), "name": "AP Operations",
                                             "tower": "AP", "description": "", "leader_id": "L-TWO"})
    assert r.status_code == 303
    dept = next(d for d in portal.store.list_departments() if d["name"] == "AP Operations")
    assert admin.post("/org/people/E-A", data={"csrf_token": _token(admin, "/org/people"), "dept_id": dept["dept_id"]}).status_code == 303
    portal.performance.refresh(["E-A"])
    assert leader.get("/people/E-A").status_code == 200
    assert leader.get("/people/E-B").status_code == 404  # outside L-TWO's departments
    assert "Emp B" not in leader.get("/people").text


# ---------------------------------------------------------------------------
# Reviews, AI insight and the development plan
# ---------------------------------------------------------------------------

def test_review_share_and_acknowledge_locks_it(portal):
    mgr = portal.login("mgr.one")
    token = _token(mgr, "/people/E-A")
    period = f"{TODAY.year}-H1"
    assert mgr.post("/people/E-A/reviews", data={"csrf_token": token, "period": period, "share": "1",
                                                 "summary": "Too short"}).status_code == 400
    r = mgr.post("/people/E-A/reviews", data={"csrf_token": token, "period": period, "rating": "4", "share": "1",
                                              "summary": "Strong half: reliable month-end delivery and good stakeholder feedback."})
    assert r.status_code == 303
    review = portal.store.list_reviews("E-A")[0]
    emp = portal.login("emp.a")
    assert emp.post(f"/employee/reviews/{review['review_id']}/acknowledge", data={"csrf_token": _token(emp), "comment": "Agreed."}).status_code == 303
    assert mgr.post("/people/E-A/reviews", data={"csrf_token": token, "period": period, "rating": "2", "share": "1",
                                                 "summary": "Changing my mind after the employee acknowledged it."}).status_code == 400
    draft = mgr.post("/people/E-A/reviews/draft", data={"csrf_token": token})
    assert draft.status_code == 200 and "Draft summary from evidence" in draft.text
    assert "full_name" not in json.dumps(CALLS["insight"][-1][1]) and "E-A" not in json.dumps(CALLS["insight"][-1][1])


def test_plan_merges_verified_ai_actions_without_duplicates(portal):
    mgr = portal.login("mgr.one")
    token = _token(mgr, "/people/E-A")
    assert mgr.post("/people/E-A/plan/generate", data={"csrf_token": token}).status_code == 303
    actions = {a["title"]: a for a in portal.store.list_actions("E-A")}
    assert actions["AI supervised practice on exceptions"]["skill"] == "Skill A"
    assert actions["Invented stretch"]["skill"] is None  # invented skill dropped, action kept
    assert "Bad type" not in actions and "Far future" not in actions
    assert any(t.startswith("Complete Skill A exception clinic (TRN-A)") for t in actions)  # real catalogue course
    count = len(actions)
    mgr.post("/people/E-A/plan/generate", data={"csrf_token": token})
    assert len(portal.store.list_actions("E-A")) == count
    emp = portal.login("emp.a")
    action = actions["AI supervised practice on exceptions"]
    assert emp.post(f"/employee/actions/{action['action_id']}/status", data={"csrf_token": _token(emp), "status": "done"}).status_code == 303
    assert portal.store.get_action(action["action_id"])["status"] == "done"
    other = portal.login("emp.b")
    assert other.post(f"/employee/actions/{action['action_id']}/status", data={"csrf_token": _token(other), "status": "open"}).status_code == 404


def test_escalates_to_manager_then_leader_then_resolves(portal):
    store, perf = portal.store, portal.performance
    store.save_review(user_id="E-B", period=f"{TODAY.year}-H1", manager_id="M-ONE", rating=1, summary="Below expectations.",
                      strengths="", improvements="Accuracy.", share=True, ai_assisted=False)
    store.add_evidence(user_id="E-B", kind="escalation", source="manual", title="Supplier escalation",
                       summary="Late payment escalated by a supplier.", occurred_on=TODAY.isoformat(), sentiment="negative",
                       created_by="M-ONE")
    store.add_actions("E-B", [{"title": f"Overdue action {n}", "action_type": "behaviour", "source": "manager",
                               "due_date": (TODAY - timedelta(days=5 + n)).isoformat()} for n in range(2)], "M-ONE")
    assert perf.status_for("E-B")["band"] == "Off track"
    levels = {e["level"] for e in store.list_escalations(user_ids=["E-B"])}
    assert levels == {"manager"}
    assert any(n["kind"] == "escalation" for n in store.list_notifications("M-ONE"))
    conn = store.conn()
    try:  # forty days later, the plan still isn't moving
        conn.execute("UPDATE employee_status SET band_since = ? WHERE user_id = 'E-B'", ((TODAY - timedelta(days=40)).isoformat(),))
        conn.execute("UPDATE development_actions SET created_at = ? WHERE user_id = 'E-B'", ((TODAY - timedelta(days=40)).isoformat(),))
        conn.commit()
    finally:
        conn.close()
    perf.refresh(["E-B"])
    assert {e["level"] for e in store.list_escalations(user_ids=["E-B"])} == {"manager", "leader"}
    assert any(n["kind"] == "escalation" for n in store.list_notifications("L-ONE"))
    # Things improve: a strong review, positive evidence, the plan done.
    store.save_review(user_id="E-B", period=f"{TODAY.year}-H2", manager_id="M-ONE", rating=5, summary="Turned it around.",
                      strengths="", improvements="", share=True, ai_assisted=False)
    for action in store.list_actions("E-B"):
        store.set_action_status(action["action_id"], "done")
    assert perf.status_for("E-B")["band"] in ("On track", "Exceeding")
    assert store.list_escalations(user_ids=["E-B"]) == []  # open ones were resolved automatically


def test_insight_route_saves_a_labelled_result(portal):
    mgr = portal.login("mgr.one")
    assert mgr.post("/people/E-A/insight", data={"csrf_token": _token(mgr, "/people/E-A")}).status_code == 303
    assert portal.store.latest_insight("E-A")["insight"]["summary"] == "Insight summary"
    assert "Insight summary" in mgr.get("/people/E-A").text


# ---------------------------------------------------------------------------
# Probation
# ---------------------------------------------------------------------------

def test_probation_from_hire_date_to_leader_decision(portal):
    admin, mgr, leader = portal.login("admin.one"), portal.login("mgr.one"), portal.login("leader.one")
    hired = (TODAY - timedelta(days=160)).isoformat()
    assert admin.post("/org/people/E-NEW", data={"csrf_token": _token(admin, "/org/people"), "hire_date": hired}).status_code == 303
    view = portal.probation.view("E-NEW", TODAY)
    assert view and len(view["checkpoints"]) == 3 and view["status"] == "active" and view["decision_due"] is True
    token = _token(mgr, "/people/E-NEW")
    r = mgr.post("/people/E-NEW/probation/recommend", data={"csrf_token": token, "recommendation": "confirm", "note": "Doing well overall in the role."})
    assert r.status_code == 400 and "at least one check-in" in r.text
    first, second, _ = view["checkpoints"]
    note = "Picks up SOPs quickly and closes items on time."
    assert mgr.post(f"/people/E-NEW/probation/checkpoints/{first['checkpoint_id']}",
                    data={"csrf_token": token, "rating": "on_track", "note": note}).status_code == 303
    r = mgr.post("/people/E-NEW/probation/recommend", data={"csrf_token": token, "recommendation": "not_confirm",
                                                            "note": "Not working out, based on one month only, which is not enough."})
    assert r.status_code == 400 and "two documented check-ins" in r.text
    mgr.post(f"/people/E-NEW/probation/checkpoints/{second['checkpoint_id']}", data={"csrf_token": token, "rating": "on_track", "note": note})
    assert portal.login("emp.new").get("/employee/performance").text.count(note) == 2  # the employee reads every check-in
    assert mgr.post("/people/E-NEW/probation/recommend", data={"csrf_token": token, "recommendation": "confirm",
                                                               "note": "Two on-track check-ins; meets the bar."}).status_code == 303
    record = portal.store.probation_for_user("E-NEW")
    assert record["status"] == "pending_leader"
    assert any(n["kind"] == "probation_decision" for n in portal.store.list_notifications("L-ONE"))
    ltoken = _token(leader, "/leader/probation")
    assert leader.post(f"/leader/probation/{record['probation_id']}/decision", data={"csrf_token": ltoken, "decision": "return"}).status_code == 400
    assert leader.post(f"/leader/probation/{record['probation_id']}/decision",
                       data={"csrf_token": ltoken, "decision": "approve", "note": ""}).status_code == 303
    assert portal.store.probation_for_user("E-NEW")["status"] == "confirmed"
    assert any(n["title"] == "Your probation is confirmed" for n in portal.store.list_notifications("E-NEW"))
    assert portal.login("emp.new").post(f"/leader/probation/{record['probation_id']}/decision",
                                        data={"csrf_token": ltoken, "decision": "approve"}).status_code in (403, 401)


# ---------------------------------------------------------------------------
# Job profiles
# ---------------------------------------------------------------------------

def _skills(portal, user_id):
    return {s["skill"]: s for s in portal.tni.employee_tni(user_id)["skills"]}


def test_manager_proposes_leader_applies_and_tni_follows_the_role(portal):
    mgr, leader = portal.login("mgr.one"), portal.login("leader.one")
    form = {"action": "add", "skill": "Python Automation", "target_level": "2", "capability": "Digital",
            "behavior_indicator": "Automates a recurring reconciliation with a documented script.",
            "reason": "Automation is now expected of associates."}
    r = mgr.post(f"/org/job-profiles/{SA}/skills", data={**form, "csrf_token": _token(mgr, "/org/job-profiles")})
    assert r.status_code == 303
    assert "Python Automation" not in _skills(portal, "E-A")  # a proposal changes nothing yet
    change = portal.store.list_skill_changes(role_id=SA, status="proposed")[0]
    assert any(n["kind"] == "job_change" for n in portal.store.list_notifications("L-ONE"))
    ltoken = _token(leader, f"/org/job-profiles/{SA}")
    assert leader.post(f"/org/changes/{change['change_id']}/decision", data={"csrf_token": ltoken, "decision": "approve"}).status_code == 303
    added = _skills(portal, "E-A")["Python Automation"]
    assert added["assessed"] is False and added["target"] == 2
    r = leader.post(f"/org/job-profiles/{SA}/skills", data={"csrf_token": ltoken, "action": "update", "skill": "Skill B",
                                                           "target_level": "4", "reason": "Controls now owned end to end."})
    assert r.status_code == 303
    skill_b = _skills(portal, "E-A")["Skill B"]
    assert skill_b["target"] == 4 and skill_b["gap"] == 2 and skill_b["severity"] == "High"  # recomputed, not the dataset's
    r = leader.post(f"/org/job-profiles/{TE}/skills", data={"csrf_token": ltoken, "action": "remove", "skill": "Skill C",
                                                           "reason": "Testing the last-skill guard."})
    assert r.status_code == 400 and "at least one required skill" in r.text
    emp = portal.login("emp.a")
    assert emp.get("/org/job-profiles").status_code == 403


def test_leader_creates_a_job_profile(portal):
    leader = portal.login("leader.one")
    r = leader.post("/org/job-profiles", data={"csrf_token": _token(leader, "/org/job-profiles"), "name": "AP Automation Analyst",
                                               "tower": "AP", "grade": "B4", "process_group": "P2P",
                                               "summary": "Builds and runs automation for invoice processing and controls."})
    assert r.status_code == 303 and r.headers["location"] == "/org/job-profiles/AP_ap_automation_analyst"
    assert portal.login("mgr.one").post("/org/job-profiles", data={"csrf_token": _token(portal.login("mgr.one"), "/org/job-profiles"),
                                                                    "name": "Nope", "tower": "AP", "grade": "B3",
                                                                    "summary": "x" * 40}).status_code == 403


# ---------------------------------------------------------------------------
# Coding tasks in the simulation
# ---------------------------------------------------------------------------

def test_resume_keyword_triggers_coding_task_reviewed_never_run(portal):
    sme = portal.login("sme.one")
    token = _token(sme, "/sme/coding")
    r = sme.post("/sme/coding", data={"csrf_token": token, "title": "Match bank lines", "language": "python",
                                      "keywords": "Python, pandas, <bad>", "difficulty": "intermediate", "time_minutes": "30",
                                      "prompt": "Write match(bank, invoices) that pairs lines by reference, then amount.",
                                      "rubric": "Reference first, then unique amount; no double matching.",
                                      "starter_code": "def match(bank, invoices):\n    pass\n"})
    assert r.status_code == 303
    challenge = portal.store.list_challenges()[0]
    assert challenge["keywords"] == ["python", "pandas"] and portal.coding.trigger_keywords() == []  # drafts never trigger
    sme.post(f"/sme/coding/{challenge['challenge_id']}/status", data={"csrf_token": token, "status": "approved"})
    assert portal.coding.trigger_keywords() == ["python", "pandas"]

    emp = portal.login("emp.c")
    r = emp.post("/employee/dashboard/upload", data={"csrf_token": _token(emp)},
                 files={"resume": ("cv.txt", b"AP associate. Python and COBOL scripts for reconciliations.", "text/plain")})
    assert r.status_code == 303
    cycle = portal.db.get_active_cycle_for_user("E-C")
    assert portal.store.resume_signals(cycle["cycle_id"])["keywords"] == ["python"]  # "cobol" isn't a trigger
    portal.db.update_cycle(cycle["cycle_id"], status="mcq_submitted", mcq_score_json=json.dumps({"by_skill": []}))
    page = emp.get(f"/employee/evidence/{cycle['cycle_id']}").text
    assert "Match bank lines" in page and 'class="kw">python' in page
    r = emp.post(f"/employee/evidence/{cycle['cycle_id']}/submit", data={"csrf_token": _token(emp)})
    assert r.status_code == 400 and "coding task" in r.text
    code = "def match(bank, invoices):\n    import os\n    return [], bank, invoices\n"
    r = emp.post(f"/employee/evidence/{cycle['cycle_id']}/code",
                 data={"csrf_token": _token(emp), "challenge_id": challenge["challenge_id"], "code": code, "paste_chars": "12"})
    assert r.status_code == 303 and CALLS["review"][-1] == code.rstrip("\n")  # indentation preserved, sent for review -- not run
    attempt = portal.store.list_attempts(cycle_id=cycle["cycle_id"])[0]
    assert attempt["review_status"] == "ai_reviewed" and attempt["paste_chars"] == 12
    emp.post(f"/employee/evidence/{cycle['cycle_id']}/code",
             data={"csrf_token": _token(emp), "challenge_id": challenge["challenge_id"], "code": code})
    assert len(portal.store.list_attempts(cycle_id=cycle["cycle_id"])) == 1  # one attempt only
    portal.db.update_cycle(cycle["cycle_id"], status="pending_manager_approval", manager_id="M-ONE")
    assert "Match bank lines" in portal.login("mgr.one").get(f"/manager/validations/{cycle['cycle_id']}").text
    assert sme.post(f"/sme/coding/attempts/{attempt['attempt_id']}/review",
                    data={"csrf_token": token, "score": "4", "note": "Good structure."}).status_code == 303
    assert portal.store.list_attempts(cycle_id=cycle["cycle_id"])[0]["review_status"] == "sme_reviewed"


# ---------------------------------------------------------------------------
# Integrations, engagement, notifications
# ---------------------------------------------------------------------------

def test_integration_ingest_requires_token_and_the_manager(portal, monkeypatch):
    client = portal.login("emp.a")
    body = {"employee_email": "emp.a@test.local", "tagged_by_email": "mgr.one@test.local", "source": "teams",
            "subject": "Teams thread", "body": "Thanks for jumping on the vendor call and resolving the dispute same day.",
            "sent_at": TODAY.isoformat()}
    assert client.post("/integrations/evidence", json=body).status_code == 404  # disabled until a token is configured
    monkeypatch.setenv("TALENT360_INTEGRATION_TOKEN", "integration-secret-123")
    assert client.post("/integrations/evidence", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    auth = {"Authorization": "Bearer integration-secret-123"}
    assert client.post("/integrations/evidence", json={**body, "tagged_by_email": "mgr.two@test.local"}, headers=auth).status_code == 403
    assert client.post("/integrations/evidence", json={**body, "employee_email": "nobody@x.y"}, headers=auth).status_code == 404
    assert client.post("/integrations/evidence", content=b"not json", headers=auth).status_code == 400
    r = client.post("/integrations/evidence", json=body, headers=auth)
    assert r.status_code == 201 and r.json()["status"] == "pending_manager_confirmation"
    assert portal.store.get_evidence(r.json()["evidence_id"])["status"] == "pending"
    assert client.post("/integrations/evidence", json=body, headers=auth).status_code == 400  # duplicate


def test_kudos_and_pulse(portal):
    emp = portal.login("emp.a")
    token = _token(emp, "/employee/performance")
    r = emp.post("/employee/kudos", data={"csrf_token": token, "recipient_id": "E-B",
                                          "message": "Covered my supplier calls during the cut-over. Thank you!"})
    assert r.status_code == 303
    kudos = portal.store.list_evidence("E-B", statuses=("pending",))
    assert kudos and kudos[0]["source"] == "peer" and kudos[0]["sentiment"] == "positive"
    assert any(n["kind"] == "kudos" for n in portal.store.list_notifications("E-B"))
    assert emp.post("/employee/kudos", data={"csrf_token": token, "recipient_id": "E-A", "message": "Me, myself and I did great."}).status_code == 400
    for mood in (4, 2):  # same week twice -> one row, the latest answer
        assert emp.post("/employee/pulse", data={"csrf_token": token, "mood": str(mood), "workload": "3"}).status_code == 303
    rows = portal.store.pulses_for_users(["E-A"], "0000")["E-A"]
    assert len(rows) == 1 and rows[0]["mood"] == 2
    assert emp.post("/employee/pulse", data={"csrf_token": token, "mood": "9", "workload": "3"}).status_code == 400


def test_wellbeing_flag_reaches_the_manager_only(portal):
    from services import engagement
    for weeks in range(3):
        portal.store.upsert_pulse(user_id="E-C", week=engagement.week_key(TODAY - timedelta(weeks=weeks)), mood=1, workload=5,
                                  comment="", shared=False)
    portal.performance.refresh(["E-C"])
    manager_view = {c["user_id"]: c for c in portal.performance.team_cards("M-ONE")}
    assert "wellbeing" in {s["key"] for s in manager_view["E-C"]["signals"]}
    leader_view = {c["user_id"]: c for c in portal.performance.directory({}, 1, viewer_role="leader")["cards"]}
    assert "wellbeing" not in {s["key"] for s in leader_view["E-C"]["signals"]}
    assert "Wellbeing check-in" not in portal.login("leader.one").get("/people/E-C").text


def test_notifications_bell_and_safe_links(portal):
    portal.store.add_notification(user_id="E-A", kind="evidence", title="Off-site link", body="", link="https://evil.example/x",
                                  channels="in_app")
    emp = portal.login("emp.a")
    page = emp.get("/employee/performance").text
    assert 'class="dot"' in page
    item = portal.store.list_notifications("E-A")[0]
    r = emp.post(f"/notifications/{item['notification_id']}/open", data={"csrf_token": _token(emp)})
    assert r.status_code == 303 and r.headers["location"] == "/notifications"
    other = portal.login("emp.b")
    assert other.post(f"/notifications/{item['notification_id']}/open", data={"csrf_token": _token(other)}).status_code == 404
    emp.post("/notifications/read", data={"csrf_token": _token(emp)})
    assert portal.store.unread_count("E-A") == 0


def test_directory_pages_and_health(portal):
    portal.performance.refresh_all()
    result = portal.performance.directory({}, 1, "name", page_size=2)
    assert result["pages"] >= 3 and len(result["cards"]) == 2 and result["total"] == 5
    assert portal.performance.directory({}, 99, "name", page_size=2)["cards"] == []
    client = portal.login("emp.a")
    assert client.get("/healthz").json() == {"status": "ok"} and client.get("/readyz").json() == {"status": "ready"}
