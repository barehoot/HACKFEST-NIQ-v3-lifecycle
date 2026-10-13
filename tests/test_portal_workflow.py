"""
End-to-end test of the portal's human-in-the-loop assessment workflow:

  employee uploads resume -> AI questions wait for SME approval -> employee
  takes the MCQ (score hidden from them) -> case-study evidence round ->
  manager review -> AI training plan unlocked

Every gate is asserted, including the negative paths (employee can't take
unapproved questions, another team's manager can't see/decide the cycle,
an admin can't skip straight to manager approval, an SME can't flip an
already-approved question).

Runs against a throwaway SQLite DB with the pipeline-API adapter stubbed,
so no network, LLM key or dataset build is needed.
"""
import importlib.util
import json
import os
import re
import sqlite3
import sys

import bcrypt
import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORTAL = ROOT / "Talent360i_phase11C_fixed_remote_hybrid"
PASSWORD = "Workflow-Test-Pass-42!"
CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')

SELF_CHECK = {k: {"score": 5, "verdict": "pass", "note": "ok"}
              for k in ("relevance", "difficulty_fit", "bloom_level", "bias")}
GENERATED = {
    "status": "auto_matched",
    "match": {"role_id": "AP_B3_01", "role_name": "P2P Senior Associate", "tower": "AP",
              "framework": "FinOps", "role_grade": "B3", "skill_count": 2, "target_question_count": 2},
    "items": [
        {"question_id": "GEN-1", "role_id": "AP_B3_01", "skill": "Invoice Processing & 3-Way Match",
         "target_proficiency_level": 3, "is_critical": True, "question_type": "Multiple Choice - Scenario",
         "question_text": "Which check protects the three-way match?", "option_a": "Release without receipt",
         "option_b": "Verify PO, receipt and invoice", "option_c": "Delete the invoice", "option_d": "Skip approval",
         "correct_option": "B", "difficulty": "Role Ready", "ai_confidence": 0.9,
         "self_check": SELF_CHECK, "generation_attempts": 1},
        {"question_id": "GEN-2", "role_id": "AP_B3_01", "skill": "AP Controls & Compliance",
         "target_proficiency_level": 2, "is_critical": False, "question_type": "Multiple Choice - Scenario",
         "question_text": "Which control reduces duplicate payments?", "option_a": "Duplicate invoice check",
         "option_b": "Office survey", "option_c": "Supplier logo", "option_d": "Leave calendar",
         "correct_option": "A", "difficulty": "Foundational", "ai_confidence": 0.8,
         "self_check": SELF_CHECK, "generation_attempts": 1},
    ],
    "skills_skipped": [], "counts": {"approved": 2, "failed": 0, "skipped": 0},
}
SCORE = {"results": [], "unanswered": [], "total_weight": 8.0, "earned_weight": 4.9,
         "overall_score_pct": 61.37, "by_skill": [
             {"skill": "Invoice Processing & 3-Way Match", "target_proficiency_level": 3, "is_critical": True,
              "correct": 1, "total": 1, "pct": 100.0},
             {"skill": "AP Controls & Compliance", "target_proficiency_level": 2, "is_critical": False,
              "correct": 0, "total": 1, "pct": 0.0}]}
CASE_STUDIES = {"role_id": "AP_B3_01", "role_name": "P2P Senior Associate", "skills_skipped": [],
                "case_studies": [{"question_id": "CS-1", "role_id": "AP_B3_01", "skill": "AP Controls & Compliance",
                                  "target_proficiency_level": 2, "is_critical": False,
                                  "question_text": "Describe a time you stopped a duplicate payment."}]}
EVIDENCE = {"role_id": "AP_B3_01", "role_name": "P2P Senior Associate", "role_grade": "B3", "evidence_results": [],
            "evidence_score_pct": 70.0, "mcq_score_pct": 61.37, "compatibility_score_pct": 65.0,
            "compatibility_band": "Developing", "manager_review_status": "Pending Manager Review"}
PROBE = "Who would you escalate the held payment to, and what evidence would you attach?"
EVIDENCE_CALLS = []
DEV_PLAN = {"status": "ok", "role_id": "AP_B3_01", "role_name": "P2P Senior Associate", "role_grade": "B3",
            "plan_summary": "Focus on AP controls.", "steps": [], "unaddressed_gaps": [], "all_confirmed_gaps": [],
            "never_assessed": [], "at_target": [], "verification": {}, "duration_estimate": {}}

USERS = [  # user_id, username, role, manager_id
    ("M-ONE", "mgr.one", "manager", None),
    ("M-TWO", "mgr.two", "manager", None),
    ("E-ONE", "emp.one", "employee", "M-ONE"),
    ("S-ONE", "sme.one", "sme", None),
    ("A-ONE", "admin.one", "admin", None),
]

_ENV = {
    "TALENT360_DEBUG": "true",  # plain-http TestClient needs non-Secure session cookies
    "TALENT360_SESSION_SECRET": "workflow-test-session-secret-0123456789",
    "TALENT360_API_MODE": "remote",
    "TALENT360_BACKEND_URL": "http://127.0.0.1:9",
    "TALENT360_DEMO_PASSWORD": PASSWORD,
    "TALENT360_SYNTHETIC_PASSWORD": PASSWORD,
}


def _purge_portal_modules():
    for name in [m for m in sys.modules if m.split(".")[0] in {"services", "domain", "agents", "routes", "portal_app"}]:
        sys.modules.pop(name)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def portal(tmp_path_factory):
    from fastapi.testclient import TestClient

    tmp = tmp_path_factory.mktemp("portal")
    saved_env = {k: os.environ.get(k) for k in [*_ENV, "TALENT360_LOG_DIR"]}
    os.environ.update(_ENV)
    os.environ.setdefault("TALENT360_LOG_DIR", str(tmp / "logs"))
    _purge_portal_modules()
    sys.path.insert(0, str(PORTAL))
    try:
        # Real schema from the seed script, in a throwaway DB.
        seed = _load("seed_users_for_test", ROOT / "scripts" / "seed_users.py")
        reference = _load("build_sqlite_for_test", ROOT / "scripts" / "build_sqlite.py")
        db_file = tmp / "talent360i.sqlite"
        conn = sqlite3.connect(db_file)
        # Empty reference tables (roles, skills, TNI gaps, catalogue) plus the workflow tables.
        for statement in reference.SCHEMA_STATEMENTS + seed.SCHEMA_STATEMENTS:
            conn.execute(statement)
        # The matched role, so the SME can build an assessment for it.
        conn.execute("INSERT INTO role_master VALUES ('AP_B3_01', 'AP', 'P2P', 'P2P Senior Associate', 'B3', 'B3')")
        for i, item in enumerate(GENERATED["items"], start=1):
            conn.execute("INSERT INTO role_skill_map (role_skill_id, role_id, skill_id, skill, capability, target_proficiency_level, "
                         "behavior_indicator, is_critical) VALUES (?, 'AP_B3_01', ?, ?, 'Technical', ?, 'ind', ?)",
                         (f"RSM-{i}", f"SK-{i}", item["skill"], item["target_proficiency_level"], int(item["is_critical"])))
        pw_hash = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
        for user_id, username, role, manager_id in USERS:
            conn.execute(
                "INSERT INTO users (user_id, username, email, password_hash, full_name, role, manager_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, username, f"{username}@test.local", pw_hash, username.replace(".", " ").title(), role, manager_id),
            )
        conn.commit()
        conn.close()

        app_module = _load("portal_app", PORTAL / "app.py")
        from services import api_client, db

        db.DB_PATH = db_file
        api_client.backend_generate_resume = lambda *a, **k: json.loads(json.dumps(GENERATED))
        api_client.backend_match_resume = lambda *a, **k: {"status": "auto_matched", "match": dict(GENERATED["match"])}
        api_client.backend_generate_for_role = lambda role_id, count, level=None, skills=None: {
            "items": json.loads(json.dumps(GENERATED["items"]))[:count], "skills_skipped": [], "match": dict(GENERATED["match"])}
        api_client.backend_score = lambda items, answers: dict(SCORE)
        api_client.backend_case_studies = lambda *a, **k: json.loads(json.dumps(CASE_STUDIES))
        api_client.backend_evidence = lambda *a, **k: EVIDENCE_CALLS.append(k) or dict(EVIDENCE)
        # The AI assessor probes once, then judges the evidence sufficient.
        api_client.backend_probe = lambda case_study, transcript: (
            {"action": "probe", "question": PROBE} if not any(t["role"] == "agent" for t in transcript)
            else {"action": "complete", "reason": "sufficient_evidence"})
        api_client.backend_devplan = lambda *a, **k: dict(DEV_PLAN)
        api_client.backend_training_outline = lambda role_id, mode, items: {
            "mode": mode, "role_id": role_id, "summary": "Two focused sessions.", "generated_by": "agent",
            "total_minutes": 60 * len(items), "skills": items,
            "sessions": [{"session_number": n, "skill": i["skill"], "title": f"Session on {i['skill']}", "format": "Workshop",
                          "duration_minutes": 60, "from_level": i["from_level"], "to_level": i["to_level"], "is_critical": False,
                          "objectives": ["o"], "activities": ["a"], "practice_task": "", "success_check": "",
                          "course_id": None, "course_title": None} for n, i in enumerate(items, 1)]}

        def login(username):
            client = TestClient(app_module.app, follow_redirects=False)
            token = CSRF.search(client.get("/login").text).group(1)
            r = client.post("/login", data={"username": username, "password": PASSWORD, "csrf_token": token})
            assert r.status_code == 303 and r.headers["location"] != "/login", username
            return client

        yield type("Portal", (), {"login": staticmethod(login), "db": db})
    finally:
        sys.path.remove(str(PORTAL))
        _purge_portal_modules()
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _csrf(client, page):
    r = client.get(page)
    assert r.status_code == 200, (page, r.status_code)
    return CSRF.search(r.text).group(1), r.text


def _status(portal, cycle_id):
    return portal.db.get_cycle(cycle_id)["status"]


def test_full_workflow_enforces_every_human_gate(portal):
    emp, sme = portal.login("emp.one"), portal.login("sme.one")
    mgr, other_mgr = portal.login("mgr.one"), portal.login("mgr.two")

    # 1. The employee only uploads a resume -- no say in how many questions. Agent 1
    # matches the role and the cycle waits for an SME to set the assessment up.
    token, page = _csrf(emp, "/employee/dashboard")
    assert "max_items" not in page
    r = emp.post("/employee/dashboard/upload", data={"csrf_token": token},
                 files={"resume": ("resume.txt", b"P2P Senior Associate, invoice processing", "text/plain")})
    assert r.status_code == 303
    cycle_id = portal.db.get_active_cycle_for_user("E-ONE")["cycle_id"]
    assert _status(portal, cycle_id) == "pending_sme_setup"
    assert emp.get(f"/employee/assessment/{cycle_id}").status_code == 404

    # 2. The SME decides the count and level; Agent 2's questions go to the bank
    # pending review. Only an SME can approve, and not twice.
    sme_token, _ = _csrf(sme, "/sme/generate")
    r = sme.post("/sme/generate", data={"csrf_token": sme_token, "role_id": "AP_B3_01", "count": "2", "level": "3"})
    assert r.status_code == 200 and "awaiting your review" in r.text
    assert emp.post("/sme/questions/GEN-1/approve", data={"csrf_token": token}).status_code == 403
    for qid in ("GEN-1", "GEN-2"):
        sme_token, _ = _csrf(sme, f"/sme/questions/{qid}")
        assert sme.post(f"/sme/questions/{qid}/approve", data={"csrf_token": sme_token}).status_code == 303
    assert sme.post("/sme/questions/GEN-1/reject", data={"csrf_token": sme_token}).status_code == 404
    assert _status(portal, cycle_id) == "pending_sme_setup"  # approved, but not deployed yet

    # 3. The selection agent proposes both from the bank; the SME deploys them to the submission.
    sme_token, builder = _csrf(sme, f"/sme/assessments/new?cycle={cycle_id}&count=2")
    assert 'value="GEN-1" checked' in builder and 'value="GEN-2" checked' in builder
    r = sme.post("/sme/assessments/deploy", data={"csrf_token": sme_token, "role_id": "AP_B3_01", "cycle_id": cycle_id,
                                                  "question_ids": ["GEN-1", "GEN-2"]})
    assert r.status_code == 303 and _status(portal, cycle_id) == "ready_for_assessment"

    # 4. MCQ: answer key is never sent to the browser, score is never shown to the employee.
    token, page = _csrf(emp, f"/employee/assessment/{cycle_id}")
    assert "correct_option" not in page and "Verify PO, receipt and invoice" in page
    r = emp.post(f"/employee/assessment/{cycle_id}/submit",
                 data={"csrf_token": token, "answers": json.dumps({"question_1": "B", "question_2": "B"})})
    assert r.status_code == 303 and _status(portal, cycle_id) == "mcq_submitted"
    assert "61.37" not in emp.get("/employee/dashboard").text

    # 5. Workplace simulation: the AI assessor probes the response, then the whole
    # transcript becomes the evidence report in the manager queue; training still locked.
    token, page = _csrf(emp, f"/employee/evidence/{cycle_id}")
    assert "Describe a time you stopped a duplicate payment." in page
    assert emp.post(f"/employee/evidence/{cycle_id}/submit", data={"csrf_token": token}).status_code == 400
    r = emp.post(f"/employee/evidence/{cycle_id}/respond",
                 data={"csrf_token": token, "question_id": "CS-1", "response": "I held the payment and added a duplicate check."})
    assert r.status_code == 303 and PROBE in emp.get(f"/employee/evidence/{cycle_id}").text
    r = emp.post(f"/employee/evidence/{cycle_id}/respond",
                 data={"csrf_token": token, "question_id": "CS-1", "response": "The AP lead, with the duplicate report attached."})
    assert r.status_code == 303 and "All scenarios complete" in emp.get(f"/employee/evidence/{cycle_id}").text
    r = emp.post(f"/employee/evidence/{cycle_id}/submit", data={"csrf_token": token})
    assert r.status_code == 303 and _status(portal, cycle_id) == "pending_manager_approval"
    transcript = EVIDENCE_CALLS[-1]["transcripts"]["CS-1"]
    assert [t["role"] for t in transcript] == ["candidate", "agent", "candidate"] and transcript[1]["text"] == PROBE

    # 6. A manager from another team can neither see nor decide this cycle.
    assert other_mgr.get(f"/manager/validations/{cycle_id}").status_code == 404
    other_token, _ = _csrf(other_mgr, "/manager/validations")
    r = other_mgr.post(f"/manager/validations/{cycle_id}/decision",
                       data={"csrf_token": other_token, "decision": "approved"})
    assert r.status_code == 403 and _status(portal, cycle_id) == "pending_manager_approval"

    # 7. The employee's own manager approves -> the AI training plan unlocks. Opening the
    # review generates (and caches) the Agent 5 plan, but the employee's page must not
    # show it until the manager has actually approved.
    token, _ = _csrf(mgr, f"/manager/validations/{cycle_id}")
    locked = emp.get("/employee/learning")
    assert locked.status_code == 200 and DEV_PLAN["plan_summary"] not in locked.text
    # One skill shown, one not -> the recommendation is to reject, so approving needs a rationale,
    # and at least one ticked skill to certify.
    review = mgr.get(f"/manager/validations/{cycle_id}").text
    assert "Recommended: Reject and assign skill-gap training" in review and 'value="Invoice Processing &amp; 3-Way Match" checked' in review
    r = mgr.post(f"/manager/validations/{cycle_id}/decision", data={"csrf_token": token, "decision": "approved"})
    assert r.status_code == 400 and "Tick at least one skill" in r.text
    r = mgr.post(f"/manager/validations/{cycle_id}/decision",
                 data={"csrf_token": token, "decision": "approved", "certify": ["Invoice Processing & 3-Way Match"]})
    assert r.status_code == 400 and "add a short rationale" in r.text and _status(portal, cycle_id) == "pending_manager_approval"
    r = mgr.post(f"/manager/validations/{cycle_id}/decision",
                 data={"csrf_token": token, "decision": "approved", "note": "Evidence reviewed.",
                       "certify": ["Invoice Processing & 3-Way Match", "Not A Skill On This Assessment"]})
    assert r.status_code == 303 and _status(portal, cycle_id) == "manager_approved"
    certs = portal.db.list_certifications(["E-ONE"])
    assert [(c["skill"], c["level"], c["certified_by"]) for c in certs] == [("Invoice Processing & 3-Way Match", 3, "M-ONE")]
    outline = portal.db.list_outlines(user_id="E-ONE")[0]
    assert outline["mode"] == "skill_up" and len(outline["event_ids"]) == len(outline["outline"]["sessions"]) == 2
    assert [i["kind"] for i in outline["items"]] == ["gap", "skill_up"]  # the unshown skill, then the next level
    unlocked = emp.get("/employee/learning")
    assert unlocked.status_code == 200 and DEV_PLAN["plan_summary"] in unlocked.text
    assert "Training from your manager's review" in unlocked.text and "Session on AP Controls" in unlocked.text


def test_admin_cannot_skip_sme_and_assessment_steps(portal):
    cycle_id = portal.db.create_cycle(
        user_id="E-ONE", resume_filename="r.txt", resume_text="Uploaded: r.txt", matched_role_id="AP_B3_01",
        matched_role_name="P2P Senior Associate", matched_role_grade="B3", match_status="auto_matched",
        status="pending_sme_approval",
    )
    admin = portal.login("admin.one")
    token, _ = _csrf(admin, "/admin/dashboard")
    r = admin.post(f"/manager/validations/{cycle_id}/decision", data={"csrf_token": token, "decision": "approved"})
    assert r.status_code == 400
    assert _status(portal, cycle_id) == "pending_sme_approval"


def test_malformed_answers_payload_is_ignored_not_a_500(portal):
    from domain import assessment

    assert assessment.parse_answers("[1, 2]") == {}
    assert assessment.parse_answers('"B"') == {}
    assert assessment.parse_answers('{"question_1": "B"}') == {1: "B"}


def test_update_cycle_rejects_unknown_columns(portal):
    with pytest.raises(ValueError):
        portal.db.update_cycle("any", **{"status = 'manager_approved' --": "x"})
