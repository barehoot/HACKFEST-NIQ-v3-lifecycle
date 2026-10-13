"""
Performance summaries and career mobility in the portal:

  - a note the manager writes is read by Agent 7 for context (never changing the record),
    with a tone check when the reading disagrees
  - Agent 9 summaries (mid-year / annual / 1:1 / promotion): the request is de-identified and
    built from every source; refs that don't exist are dropped; a summary can start a review;
    HR reads but doesn't generate
  - aspirations to roles in another vertical: gap analysis from validated / assessed levels,
    then CV claims; promotion readiness ignores CV claims
  - the CV: only the claim and a short quote are stored, only for the skills that matter
  - upskilling courses land on the calendar once; applications notify the line manager and the
    hiring contact; a hiring manager shortlists or declines with a note
  - the talent pool: opted-in candidates show performance, potential fits don't (unless the
    viewer already has scope); every talent-card view is audited
  - internal postings: demo feed, title auto-mapping, an HR mapping survives re-sync, and the
    live adapter only talks https to smartrecruiters.com

Uses the lifecycle suite's throwaway database and stubbed pipeline API.
"""
import json
import sqlite3
from datetime import timedelta

import pytest

from test_lifecycle import CALLS, RESUME, SA, SUMMARY, TODAY, _token, portal  # noqa: F401 -- portal: the shared fixture

DNA_ROLE = "DNA_data_analyst"


@pytest.fixture(scope="module", autouse=True)
def data_role(portal):
    """A Data Analyst job profile in a new Data & Analytics vertical, with a Python course."""
    admin = {"id": "A-ONE", "role": "admin"}
    jp = portal.job_profiles
    jp.create_vertical(code="DNA", name="Data & Analytics", description="", user=admin)
    jp.add_catalogue_skill(skill="Python Programming", tower="DNA", capability="Digital", indicators=[], user=admin)
    jp.create_role(name="Data Analyst", tower="DNA", process_group="Data", grade="B4", dept_id=None,
                   summary="Turns finance-operations data into insight with SQL, Python and dashboards.",
                   responsibilities="", skills_text="", user=admin)
    for skill, level, critical in (("Python Programming", 3, True), ("Skill B", 2, False)):
        jp.request_change(DNA_ROLE, action="add", skill=skill, form={"target_level": level, "is_critical": critical,
                                                                     "behavior_indicator": "Applies it independently to real work."},
                          reason="Test data for mobility.", trend_id=None, user=admin)
    python = portal.store.skill_by_name("Python Programming")
    conn = sqlite3.connect(portal.db_file)
    conn.execute("INSERT INTO training_catalogue VALUES ('TRN-PY', 'DNA', 'Python for Analysts', 'Lab', 'B4', 'Low', 'DS')")
    conn.execute("INSERT INTO training_skill_map VALUES ('TSM-PY', 'TRN-PY', 'DNA', ?, 'Python Programming', 'Digital', 'B4', 'High')",
                 (python["skill_id"],))
    conn.commit()
    conn.close()
    portal.performance.refresh(["E-A", "E-B", "E-C", "E-NEW", "E-OTHER"])
    return DNA_ROLE


# ---------------------------------------------------------------------------
# Evidence read by Agent 7, and Agent 9 summaries
# ---------------------------------------------------------------------------

def test_manual_note_is_evaluated_by_agent7_without_changing_it(portal):
    mgr = portal.login("mgr.one")
    r = mgr.post("/people/E-A/evidence", data={"csrf_token": _token(mgr, "/people/E-A"), "kind": "concern", "title": "Late pack",
                                               "summary": "The month-end pack arrived a day late and needed rework.", "shared": "1"})
    assert r.status_code == 303 and CALLS["classify_source"][-1] == "manual"
    item = next(e for e in portal.store.list_evidence("E-A") if e["title"] == "Late pack")
    assert item["kind"] == "concern" and item["sentiment"] == "negative"  # the manager's record stands
    assert item["ai"]["mode"] == "evaluation" and item["ai"]["skills"] == ["Skill A"]  # role skills only
    assert "tone_check" in item["ai"]  # the stub reads it as appreciation
    page = mgr.get("/people/E-A").text
    assert "AI context" in page and "Agent 7 reads this as appreciation" in page
    assert "AI context" not in portal.login("emp.a").get("/employee/performance").text


def test_summary_is_de_identified_cites_real_evidence_and_starts_a_review(portal):
    mgr = portal.login("mgr.one")
    token = _token(mgr, "/people/E-A")
    r = mgr.post("/people/E-A/summaries", data={"csrf_token": token, "review_type": "annual"})
    assert r.status_code == 303 and r.headers["location"].endswith("summary=annual#summary")
    sent = CALLS["summary"][-1]
    text = json.dumps(sent)
    assert "E-A" not in text and "Emp A" not in text and "emp.a@" not in text
    assert sent["review_type"] == "annual" and {e["source"] for e in sent["evidence"]} >= {"manual"}
    assert all(e["ref"].startswith("E") for e in sent["evidence"])
    stored = portal.store.latest_summaries("E-A")["annual"]
    assert stored["summary"]["highlights"][0]["refs"] == ["E1"]  # the made-up E99 is dropped
    assert stored["facts"]["evidence"][0]["evidence_id"].startswith("EVD")
    page = mgr.get("/people/E-A?summary=annual").text
    assert "Fact sheet" in page and "A steady period." in page and 'href="#ev-' in page
    r = mgr.post(f"/people/E-A/summaries/{stored['summary_id']}/use", data={"csrf_token": token})
    assert r.status_code == 200 and f'value="{TODAY.year}-FY"' in r.text and "Solid delivery with one escalation." in r.text
    hr = portal.login("hr.one")
    assert "A steady period." in hr.get("/people/E-A?summary=annual").text
    assert hr.post("/people/E-A/summaries", data={"csrf_token": _token(hr), "review_type": "annual"}).status_code == 403
    assert mgr.post("/people/E-A/summaries", data={"csrf_token": token, "review_type": "weekly"}).status_code == 400


def test_promotion_summary_targets_an_aspiration_in_another_vertical(portal):
    portal.store.add_aspiration(user_id="E-B", role_id=DNA_ROLE, posting_id=None, note="Into data")
    mgr = portal.login("mgr.one")
    r = mgr.post("/people/E-B/summaries", data={"csrf_token": _token(mgr, "/people/E-B"), "review_type": "promotion",
                                                "target_role_id": DNA_ROLE})
    assert r.status_code == 303
    promotion = CALLS["summary"][-1]["promotion"]
    assert promotion["target_role"] == "Data Analyst" and promotion["cross_department"] is True
    assert promotion["level"] in ("Ready now", "Ready in 6-12 months", "Not yet", "On hold")


# ---------------------------------------------------------------------------
# Aspirations, CV claims, upskilling
# ---------------------------------------------------------------------------

def test_aspiration_gap_analysis_and_cv_claims(portal):
    emp = portal.login("emp.a")
    token = _token(emp, "/employee/career")
    assert emp.post("/employee/career/aspirations", data={"csrf_token": token, "role_id": DNA_ROLE, "note": "Analytics"}).status_code == 303
    again = emp.post("/employee/career/aspirations", data={"csrf_token": token, "role_id": DNA_ROLE})
    assert again.status_code == 303 and "already aspire" in emp.get("/employee/career").text
    emp.post("/employee/career/aspirations", data={"csrf_token": token, "role_id": SA})
    assert "the role you hold today" in emp.get("/employee/career").text

    analysis = portal.mobility.gap_analysis("E-A", DNA_ROLE)
    rows = {r["skill"]: r for r in analysis["rows"]}
    assert rows["Skill B"]["source"] == "assessed" and rows["Skill B"]["met"]
    assert rows["Python Programming"]["level"] is None and rows["Python Programming"]["course"]["course_id"] == "TRN-PY"
    assert analysis["cross_vertical"] and analysis["pct"] == 50

    RESUME.update(generated_by="agent", claims=[
        {"skill": "python programming", "level": 3, "quote": "Built Python scripts for reconciliations"},
        {"skill": "Telepathy", "level": 5, "quote": "x"}, {"skill": "Skill B", "level": 9, "quote": "y"}])
    r = emp.post("/employee/career/cv", data={"csrf_token": token},
                 files={"resume": ("cv.txt", b"Built Python scripts for reconciliations. Long CV text that must never be stored.", "text/plain")})
    assert r.status_code == 303
    data, name, skills = CALLS["resume"][-1]
    assert "Python Programming" in skills and "Skill A" in skills  # current role + aspirations
    claims = portal.store.cv_claims(["E-A"])["E-A"]
    assert [(c["skill"], c["level"]) for c in claims] == [("Python Programming", 3)]
    conn = sqlite3.connect(portal.db_file)
    assert not conn.execute("SELECT 1 FROM cv_skill_claims WHERE quote LIKE '%never be stored%'").fetchone()
    assert not conn.execute("SELECT 1 FROM assessment_cycles WHERE resume_text LIKE '%never be stored%'").fetchone()
    conn.close()
    analysis = portal.mobility.gap_analysis("E-A", DNA_ROLE)
    assert analysis["pct"] == 100 and analysis["assessed_pct"] == 50 and analysis["cv_only"] == 1
    assert analysis["promotion"]["level"] != "Ready now"  # readiness never rests on a CV claim
    page = emp.get("/employee/career").text
    assert "CV claim" in page and "Built Python scripts for reconciliations" in page


def test_upskilling_lands_on_the_calendar_once(portal):
    emp = portal.login("emp.a")
    token = _token(emp, "/employee/career")
    portal.store.clear_cv_claims("E-A")
    start = (TODAY + timedelta(days=7)).isoformat()
    r = emp.post("/employee/career/upskill", data={"csrf_token": token, "role_id": DNA_ROLE, "start_date": start, "cadence_days": "7"})
    assert r.status_code == 303
    events = [e for e in portal.schedule.events_for_user("E-A") if e["course_id"] == "TRN-PY"]
    assert len(events) == 1 and "Upskilling for Data Analyst" in events[0]["notes"]
    emp.post("/employee/career/upskill", data={"csrf_token": token, "role_id": DNA_ROLE, "start_date": start, "cadence_days": "7"})
    assert len([e for e in portal.schedule.events_for_user("E-A") if e["course_id"] == "TRN-PY"]) == 1
    assert any("upskilling" in n["title"] for n in portal.store.list_notifications("M-ONE"))
    assert emp.post("/employee/career/upskill", data={"csrf_token": token, "role_id": "AP_te", "start_date": start}).status_code == 404


# ---------------------------------------------------------------------------
# Applications and the talent pool
# ---------------------------------------------------------------------------

def test_application_notifies_and_the_hiring_side_decides(portal):
    emp = portal.login("emp.a")
    token = _token(emp, "/employee/career")
    emp.post("/employee/career/apply", data={"csrf_token": token, "role_id": DNA_ROLE, "note": "short"})
    assert not portal.store.list_applications(user_id="E-A")
    assert emp.post("/employee/career/apply", data={"csrf_token": token, "role_id": DNA_ROLE,
                                                    "note": "My automation work is a good base for analytics."}).status_code == 303
    application = portal.store.list_applications(user_id="E-A")[0]
    assert application["status"] == "submitted" and application["readiness"]["pct"] is not None
    assert any("applied internally" in n["title"] for n in portal.store.list_notifications("M-ONE"))
    assert any("Internal application" in n["title"] for n in portal.store.list_notifications("H-ONE"))  # no dept leader -> HR
    emp.post("/employee/career/apply", data={"csrf_token": token, "role_id": DNA_ROLE, "note": "Applying a second time, please."})
    assert len(portal.store.list_applications(user_id="E-A")) == 1

    hiring = portal.login("mgr.two")
    pool = hiring.get(f"/talent?role={DNA_ROLE}").text
    assert "Emp A" in pool and "Elsewhere in the organisation" in pool
    card = hiring.get(f"/talent/{DNA_ROLE}/E-A")
    assert card.status_code == 200 and "My automation work" in card.text and "Open the full 360 profile" not in card.text
    from domain import governance
    assert any(e["action"] == "talent_card_viewed" and e["actor_id"] == "M-TWO" for e in governance.get_audit_events())
    ctoken = _token(hiring, f"/talent/{DNA_ROLE}/E-A")
    url = f"/talent/applications/{application['application_id']}/decision"
    hiring.post(url, data={"csrf_token": ctoken, "decision": "declined", "note": ""})
    assert portal.store.get_application(application["application_id"])["status"] == "submitted"  # a decline needs a reason
    assert hiring.post(url, data={"csrf_token": ctoken, "decision": "shortlisted", "note": "Let's talk"}).status_code == 303
    assert portal.store.get_application(application["application_id"])["status"] == "shortlisted"
    assert any("shortlisted" in n["title"] for n in portal.store.list_notifications("E-A"))
    assert portal.login("emp.b").get("/talent").status_code == 403
    assert emp.post(url, data={"csrf_token": token, "decision": "declined", "note": "nope nope nope"}).status_code == 403


def test_potential_fits_show_no_performance_outside_scope(portal):
    portal.store.replace_cv_claims("E-C", [{"skill": "Python Programming", "level": 3, "quote": "Python daily"}], "agent", "cv.pdf")
    hiring = portal.login("mgr.two")
    assert "Emp C" not in hiring.get(f"/talent?role={DNA_ROLE}").text  # not opted in
    pool = portal.mobility.talent_pool(DNA_ROLE, {"id": "M-TWO", "role": "manager", "name": "Mgr Two"}, include_potential=True)
    c = next(r for r in pool["elsewhere"] if r["user_id"] == "E-C")
    assert c["potential"] and c["band"] is None and c["promotion"] is None
    card = hiring.get(f"/talent/{DNA_ROLE}/E-C").text
    assert "Performance private" in card and "Python daily" not in card  # no CV quotes for someone who didn't opt in
    own = portal.mobility.talent_pool(DNA_ROLE, {"id": "M-ONE", "role": "manager", "name": "Mgr One"}, include_potential=True)
    assert next(r for r in own["team"] if r["user_id"] == "E-C")["show_performance"]  # their own manager
    assert portal.login("hr.one").get(f"/talent?role={DNA_ROLE}&potential=1").status_code == 200
    assert hiring.get(f"/talent/{DNA_ROLE}/E-OTHER").status_code == 404  # no recorded fit and no interest: not in the pool


def test_cross_vertical_assessment_and_nomination_need_an_aspiration(portal):
    mgr = portal.login("mgr.one")
    token = _token(mgr, "/people/E-C")
    assert mgr.post("/people/E-C/career/nominate", data={"csrf_token": token, "role_id": DNA_ROLE, "note": "x" * 30}).status_code == 404
    portal.store.add_aspiration(user_id="E-C", role_id=DNA_ROLE, posting_id=None, note="")
    r = mgr.post("/people/E-C/career/assess", data={"csrf_token": token, "role_id": DNA_ROLE})
    assert r.status_code == 400 and "No SME-approved questions for Data Analyst" in r.text
    r = mgr.post("/people/E-C/career/nominate", data={"csrf_token": token, "role_id": DNA_ROLE, "note": "Strong analytics potential here."})
    assert r.status_code == 400 and "at least one assessment has been approved" in r.text
    assert portal.login("mgr.two").post("/people/E-C/career/assess", data={"csrf_token": token, "role_id": DNA_ROLE}).status_code in (403, 404)


# ---------------------------------------------------------------------------
# Internal postings (SmartRecruiters)
# ---------------------------------------------------------------------------

def test_demo_postings_sync_map_and_survive_resync(portal):
    sr = portal.smartrecruiters
    result = sr.sync(force=True)
    assert result["source"] == "demo" and result["count"] == 6
    postings = {p["posting_id"]: p for p in portal.store.list_postings()}
    assert postings["DEMO-1003"]["role_id"] == "AP_lead" and postings["DEMO-1001"]["role_id"] == DNA_ROLE
    assert postings["DEMO-1005"]["role_id"] is None and "Power Automate" in postings["DEMO-1005"]["description"]
    hr = portal.login("hr.one")
    token = _token(hr, "/org/jobs")
    assert hr.post("/org/jobs/DEMO-1005/map", data={"csrf_token": token, "role_id": "AP_te"}).status_code == 303
    sr.sync(force=True)
    assert portal.store.get_posting("DEMO-1005")["role_id"] == "AP_te"  # HR's mapping wins over the title match
    prefill = hr.get("/org/onboard?posting=DEMO-1006").text
    assert "Prefilled from the internal posting" in prefill and 'name="posting_id" value="DEMO-1006"' in prefill
    assert portal.login("mgr.one").get("/org/jobs").status_code == 403
    page = portal.login("emp.a").get("/jobs/DEMO-1001").text
    assert "Skills it needs" in page and "Python Programming" in page


def test_live_adapter_parses_postings_and_only_talks_to_smartrecruiters(portal, monkeypatch):
    sr = portal.smartrecruiters
    raw = {"id": "743999", "name": "Data Analyst", "releasedDate": "2026-10-01T09:00:00.000Z",
           "location": {"city": "Pune", "country": "in", "remote": True}, "department": {"label": "Data & Analytics"},
           "experienceLevel": {"label": "Associate"}, "company": {"identifier": "NIQ"}}
    detail = {"jobAd": {"sections": {"jobDescription": {"text": "<p>Build <b>SQL</b> data sets.</p><script>alert(1)</script>"},
                                     "qualifications": {"text": "<ul><li>Python</li><li>Power BI &amp; DAX</li></ul>"}}}}
    parsed = sr.parse_posting(raw, detail, "smartrecruiters")
    assert parsed["posting_id"] == "SR-743999" and parsed["location"] == "Pune, in (remote)"
    assert parsed["description"] == "Build SQL data sets." and "alert" not in parsed["description"]
    assert parsed["qualifications"] == "Python\nPower BI & DAX"
    assert parsed["apply_url"] == "https://jobs.smartrecruiters.com/NIQ/743999"
    for bad in ("http://api.smartrecruiters.com", "https://evil.example.com", "https://smartrecruiters.com.evil.io"):
        monkeypatch.setenv("SMARTRECRUITERS_API_BASE", bad)
        with pytest.raises(sr.SmartRecruitersError):
            sr._get("/v1/companies/NIQ/postings")
    monkeypatch.setenv("SMARTRECRUITERS_API_BASE", "https://api.smartrecruiters.com")
    monkeypatch.setenv("SMARTRECRUITERS_COMPANY_ID", "NIQ")
    pages = iter([{"content": [raw], "totalFound": 1}, detail])
    monkeypatch.setattr(sr, "_get", lambda path, params=None: next(pages))
    result = sr.sync(force=True)
    assert result["source"] == "smartrecruiters" and result["count"] == 1 and result["mapped"] == 1
    assert portal.store.get_posting("SR-743999")["role_id"] == DNA_ROLE
    assert portal.store.get_posting("DEMO-1001")["status"] == "open"  # a different source isn't closed by this sync
