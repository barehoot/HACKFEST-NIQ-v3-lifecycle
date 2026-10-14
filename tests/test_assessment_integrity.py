"""
Proctored assessments: the strike rules, the risk and penalty they add up to, and the
endpoint the assessment page reports to (services/integrity.py, static/js/proctor.js).

A page can't stop someone leaving it; it can record when they do. These tests pin
down what counts as a strike, that the attempt ends at the limit (in the MCQ or the
simulation, or on return after closing the page), what the manager is shown, and the
penalties: points off at medium risk; at high risk a send-back recommendation, a
written override to certify, a retake cool-off and an HR alert on a repeat.
"""
import json
from datetime import date, timedelta

import pytest

from test_portal_tni_schedule import PASSWORD, SIM_CASE, _cycle, _token, portal  # noqa: F401 (portal is the shared fixture)

ANSWERED = {"mode": "simulation", "completed": {}, "transcripts": {"CS-9": [{"role": "candidate", "text": "I would check the ledger."}]}}


def _employee(portal, username, job_title=""):
    return portal.db.create_user(username=username, email=f"{username}@test.local", password=PASSWORD, job_title=job_title,
                                 full_name=username.replace(".", " ").title(), role="employee", manager_id="M-ONE")["user_id"]


def _report(client, cycle_id, token, stage, *events, status=200):
    r = client.post(f"/employee/integrity/{cycle_id}", headers={"Content-Type": "application/json"},
                    content=json.dumps({"csrf_token": token, "stage": stage, "events": list(events)}))
    assert r.status_code == status, r.text
    return r.json() if status == 200 else r


@pytest.fixture
def svc(portal, monkeypatch):
    from services import api_client, integrity, lifecycle_store, progression, question_bank

    monkeypatch.setattr(api_client, "BACKGROUND_EVIDENCE_RETRY", False)
    monkeypatch.setattr(api_client, "backend_evidence", lambda *a, **k: {"evidence_results": [], "evidence_score_pct": 70.0})
    monkeypatch.setattr(api_client, "backend_score", lambda items, answers: {"overall_score_pct": 40.0, "by_skill": []})
    return type("Svc", (), {"api": api_client, "integrity": integrity, "store": lifecycle_store,
                            "progression": progression, "bank": question_bank, "monkeypatch": monkeypatch})


def test_both_assessment_pages_are_proctored(portal, svc):
    uid = _employee(portal, "proc.pages")
    cycle_id = _cycle(portal, uid, "ready_for_assessment")
    page = portal.login("proc.pages").get(f"/employee/assessment/{cycle_id}").text
    assert 'id="proctor-overlay"' in page and "js/proctor.js" in page and f"/employee/integrity/{cycle_id}" in page
    assert "I will complete this assessment myself" in page and "3 strikes your attempt is submitted automatically" in page


def test_what_counts_as_a_strike(portal, svc):
    uid = _employee(portal, "proc.rules")
    cycle_id = _cycle(portal, uid, "mcq_submitted", case_study_json=SIM_CASE)
    client = portal.login("proc.rules")
    token, page = _token(client, f"/employee/evidence/{cycle_id}")
    assert 'id="proctor-overlay"' in page

    assert _report(client, cycle_id, token, "simulation", {"kind": "rules_accepted", "detail": {"extended": False}})["strikes"] == 0
    brief = _report(client, cycle_id, token, "simulation", {"kind": "focus_lost", "seconds": 1.2})
    assert brief["strikes"] == 0 and brief["warning"] is None  # under the grace period: a notification, say
    # Leaving also drops full screen: one episode, one strike.
    left = _report(client, cycle_id, token, "simulation", {"kind": "focus_lost", "seconds": 9, "detail": {"via": "window"}},
                   {"kind": "fullscreen_exit"})
    assert left["strikes"] == 1 and left["warning"]["kind"] == "focus_lost" and not left["limit"]
    junk = _report(client, cycle_id, token, "simulation", {"kind": "made_up"}, {"kind": "paste_blocked", "detail": {"via": "<b>x"}})
    assert junk["strikes"] == 1

    events = svc.integrity.timeline(cycle_id)
    assert [(e["kind"], e["severity"]) for e in events] == [
        ("rules_accepted", "info"), ("focus_lost", "minor"), ("focus_lost", "strike"), ("fullscreen_exit", "minor"),
        ("paste_blocked", "minor")]
    assert events[-1]["detail"] == {}  # nothing free-form from the browser is kept
    summary = svc.integrity.summary(cycle_id)
    assert summary["risk"] == "medium" and summary["strikes"] == 1 and summary["screen"] == "single"


def test_the_strike_limit_submits_the_simulation_and_tells_the_manager(portal, svc):
    svc.monkeypatch.setattr(svc.integrity, "EPISODE_SECONDS", 0)
    uid = _employee(portal, "proc.limit")
    cycle_id = _cycle(portal, uid, "mcq_submitted", mcq_score_json={"overall_score_pct": 80, "by_skill": []},
                      case_study_json=SIM_CASE, case_study_answers_json=ANSWERED)
    client = portal.login("proc.limit")
    token, _ = _token(client, f"/employee/evidence/{cycle_id}")
    first = _report(client, cycle_id, token, "simulation", {"kind": "fullscreen_exit"})
    assert first["strikes"] == 1 and first["warning"]["label"] == "Exited full screen"
    _report(client, cycle_id, token, "simulation", {"kind": "left_page", "seconds": 4})
    third = _report(client, cycle_id, token, "simulation", {"kind": "multi_screen", "detail": {"extended": True}})
    assert third["limit"] and third["redirect"] == "/employee/dashboard"

    cycle = portal.db.get_cycle(cycle_id)
    assert cycle["status"] == "pending_manager_approval" and cycle["evidence_report"]["evidence_score_pct"] == 70.0
    assert cycle["case_study_answers"]["completed"]["CS-9"] == "integrity_limit"
    summary = svc.integrity.summary(cycle_id)
    assert summary["risk"] == "high" and summary["auto_submitted"] and summary["screen"] == "extended"
    assert any("submitted automatically" in n["title"] for n in svc.store.list_notifications("M-ONE"))
    assert _report(client, cycle_id, token, "simulation", {"kind": "focus_lost", "seconds": 30}) == {"ignored": True}
    assert "submitted automatically" in client.get("/employee/dashboard").text


def test_the_strike_limit_during_the_mcq_ends_the_attempt(portal, svc):
    svc.monkeypatch.setattr(svc.integrity, "EPISODE_SECONDS", 0)
    uid = _employee(portal, "proc.mcq")
    cycle_id = _cycle(portal, uid, "ready_for_assessment")
    client = portal.login("proc.mcq")
    token, _ = _token(client, f"/employee/assessment/{cycle_id}")
    for _ in range(2):
        _report(client, cycle_id, token, "mcq", {"kind": "focus_lost", "seconds": 20})
    last = _report(client, cycle_id, token, "mcq", {"kind": "focus_lost", "seconds": 20})
    assert last["limit"] and last["action"] == "submit_mcq"
    r = client.post(f"/employee/assessment/{cycle_id}/submit", data={"csrf_token": token, "answers": "{}"})
    assert r.status_code == 303 and r.headers["location"] == "/employee/dashboard"  # no simulation round
    assert portal.db.get_cycle(cycle_id)["status"] == "pending_manager_approval"


def test_closing_the_page_at_the_limit_still_ends_the_attempt(portal, svc):
    svc.monkeypatch.setattr(svc.integrity, "EPISODE_SECONDS", 0)
    uid = _employee(portal, "proc.closed")
    cycle_id = _cycle(portal, uid, "ready_for_assessment")
    client = portal.login("proc.closed")
    token, _ = _token(client, f"/employee/assessment/{cycle_id}")
    assert _report(client, cycle_id, token, "mcq", {"kind": "left_page"}, {"kind": "reopened"}, {"kind": "page_loaded"})["strikes"] == 1
    assert _report(client, cycle_id, token, "mcq", {"kind": "reopened"}, {"kind": "page_loaded"})["strikes"] == 2  # no beacon arrived
    assert _report(client, cycle_id, token, "mcq", {"kind": "left_page"})["action"] == "submit_mcq"
    r = client.get(f"/employee/assessment/{cycle_id}")  # back after closing the page instead of submitting
    assert r.status_code == 303 and portal.db.get_cycle(cycle_id)["status"] == "pending_manager_approval"


def test_the_endpoint_checks_the_token_the_owner_and_the_stage(portal, svc):
    uid = _employee(portal, "proc.guard")
    mine = _cycle(portal, uid, "ready_for_assessment")
    theirs = _cycle(portal, "E-OTHER", "ready_for_assessment")
    client = portal.login("proc.guard")
    token, _ = _token(client, f"/employee/assessment/{mine}")
    _report(client, mine, "not-the-token", "mcq", {"kind": "focus_lost", "seconds": 9}, status=403)
    assert _report(client, theirs, token, "mcq", {"kind": "focus_lost", "seconds": 9}) == {"ignored": True}
    assert _report(client, mine, token, "simulation", {"kind": "focus_lost", "seconds": 9}) == {"ignored": True}
    _report(client, mine, token, "exam", {"kind": "focus_lost"}, status=400)
    assert client.post(f"/employee/integrity/{mine}", content=b"{not json", headers={"Content-Type": "application/json"}).status_code == 400
    assert client.post(f"/employee/integrity/{mine}", content=b"x" * 20_000).status_code == 413
    assert svc.integrity.summary(theirs) is None and svc.integrity.timeline(mine) == []


def test_medium_risk_takes_points_off_and_high_risk_recommends_sending_back(portal, svc):
    score = {"overall_score_pct": 75.0, "by_skill": [{"skill": "Skill Alpha", "target_proficiency_level": 3,
                                                     "is_critical": False, "correct": 3, "total": 4, "pct": 75.0}]}
    medium_uid = _employee(portal, "proc.medium")
    medium = _cycle(portal, medium_uid, "pending_manager_approval", mcq_score_json=score)
    assert svc.progression.assessment_outcome(portal.db.get_cycle(medium))["rows"][0]["demonstrated"]  # 75% passes
    svc.integrity.record(portal.db.get_cycle(medium), "mcq", [{"kind": "focus_lost", "seconds": 20}])
    outcome = svc.progression.assessment_outcome(portal.db.get_cycle(medium))
    row = outcome["rows"][0]
    assert outcome["proctor_risk"] == "medium" and row["mcq_judged"] == 65.0 and not row["demonstrated"]
    assert any("points off" in r for r in outcome["reasons"])

    high_uid = _employee(portal, "proc.high", job_title="Test Preparer")  # a role on file, so a retake would be due
    high = _cycle(portal, high_uid, "pending_manager_approval", mcq_score_json={**score, "by_skill": [{**score["by_skill"][0], "pct": 100.0}]})
    svc.integrity.record(portal.db.get_cycle(high), "mcq", [{"kind": "multi_screen", "detail": {"extended": True}}])
    outcome = svc.progression.assessment_outcome(portal.db.get_cycle(high))
    assert outcome["recommendation"] == "sent_back" and not outcome["rows"][0]["demonstrated"]
    assert outcome["reasons"][0].startswith("assessment-integrity breach")
    with pytest.raises(svc.progression.DecisionError, match="30 characters"):
        svc.progression.decide(high, "approved", "Looks fine to me.", "M-ONE", certify=["Skill Alpha"])
    assert svc.store.cycles_for_users([high_uid])[high_uid][0]["integrity_risk"] == "high"  # the status engine's -15

    # Sending it back holds the retake for the cool-off, and the upload route too.
    svc.progression.decide(high, "sent_back", "", "M-ONE")
    opens = svc.integrity.cool_off_until(high_uid)
    assert opens == date.today() + timedelta(days=svc.integrity.COOL_OFF_DAYS)
    need = svc.bank.baseline_need(high_uid)
    assert not need["needs"] and "cool-off" in need["reason"]
    page = portal.login("proc.high").get("/employee/dashboard").text
    assert "Assessment-integrity cool-off" in page and 'name="resume"' not in page


def test_a_second_high_risk_attempt_alerts_hr(portal, svc):
    hr = portal.db.create_user(username="hr.integrity", email="hr.integrity@test.local", password=PASSWORD,
                               full_name="Hr Integrity", role="hr")["user_id"]
    uid = _employee(portal, "proc.repeat")
    earlier = _cycle(portal, uid, "manager_rejected", manager_decision="sent_back")
    svc.integrity.record(portal.db.get_cycle(earlier), "mcq", [{"kind": "multi_screen"}])
    latest = _cycle(portal, uid, "pending_manager_approval", mcq_score_json={"overall_score_pct": 50, "by_skill": []})
    svc.integrity.record(portal.db.get_cycle(latest), "mcq", [{"kind": "focus_lost", "seconds": 90}])
    result = svc.progression.decide(latest, "sent_back", "", "M-ONE")
    assert result["hr_alerted"]
    assert any("Repeated assessment-integrity breach" in n["title"] for n in svc.store.list_notifications(hr))


def test_typing_signals_spot_inserted_and_transcribed_answers(svc):
    typed = {"typed_chars": 300, "corrections": 0, "long_pauses": 0}
    cycle = {"case_study_answers": {"mode": "simulation", "transcripts": {"CS-1": [
        {"role": "candidate", "text": "x" * 300, "meta": typed},                                          # steady, no edits
        {"role": "candidate", "text": "y" * 300, "meta": {"typed_chars": 20, "inserted_chars": 0}},         # appeared untyped
        {"role": "candidate", "text": "z" * 300, "meta": {"typed_chars": 330, "corrections": 9, "long_pauses": 2}},
        {"role": "candidate", "text": "answered before typing was measured"},
        {"role": "agent", "text": "Who do you tell?"}]}}}
    assert svc.integrity.typing_signals(cycle) == {"measured": 3, "inserted": 1, "transcribed": 1}
    assert svc.integrity.summarize([], {"measured": 2, "inserted": 0, "transcribed": 2})["risk"] == "medium"
    assert svc.integrity.summarize([], {"measured": 2, "inserted": 0, "transcribed": 1})["risk"] == "low"


def test_the_manager_sees_the_integrity_record(portal, svc):
    uid = _employee(portal, "proc.record")
    cycle_id = _cycle(portal, uid, "pending_manager_approval", mcq_score_json={"overall_score_pct": 90, "by_skill": []})
    svc.integrity.record(portal.db.get_cycle(cycle_id), "mcq", [{"kind": "rules_accepted", "detail": {"extended": False}},
                                                                {"kind": "multi_screen", "detail": {"extended": True}}])
    mgr = portal.login("mgr.one")
    page = mgr.get(f"/manager/validations/{cycle_id}").text
    assert "ASSESSMENT INTEGRITY" in page and "Integrity breach" in page and "Second display detected" in page
    assert "Send back to retake (assessment-integrity breach)" in page and "at least 30 characters" in page
    assert "Proctoring: High" in mgr.get("/manager/validations").text
