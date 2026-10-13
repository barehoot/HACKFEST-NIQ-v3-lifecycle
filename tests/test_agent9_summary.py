"""
Agent 9 (performance summary), Agent 8's CV reader, Agent 7 on manager notes, and their endpoints:

  - Agent 9: a verified summary is returned; refs that aren't in the record, a rating outside the
    band's range, a promotion view that differs from the rules, a 1:1 without talking points, or
    employment-decision language fail verification -> one revision -> a labelled template built
    from the same record (which never invents a ref)
  - CV reader: claims only for the listed skills, levels 1-5, quotes copied verbatim from the CV;
    otherwise keyword rules; the CV text is never returned
  - Agent 7 accepts a manager's own note ("manual")
  - /api/performance-summary and /api/resume-skills require the bearer token and validate input

No network or LLM: call_llm_json is replaced in each agent module.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))
sys.path.insert(0, str(ROOT))

from test_lifecycle_agents import AUTH, _replies, client  # noqa: E402,F401  (client is the shared API fixture)

import agent7_evidence as a7  # noqa: E402
import agent8_skill_advisor as a8  # noqa: E402
import agent9_performance_summary as a9  # noqa: E402

RECORD = {
    "review_type": "mid_year", "band": "On track", "score": 70, "window": {"from": "2026-04-13", "to": "2026-10-13"},
    "role_name": "P2P Senior Associate",
    "factors": [{"name": "Skills vs role target", "score": 68, "detail": "3 of 5 at target"}],
    "reviews": [{"period": "2026-H1", "rating": 3, "summary": "Reliable on invoice processing."}],
    "evidence": [
        {"ref": "E1", "kind": "achievement", "sentiment": "positive", "source": "manual", "date": "2026-09-01",
         "title": "Cut duplicate-payment exceptions by 40%", "context": "Vendor-master clean-up.", "skills": ["Skill A"], "weight": 3},
        {"ref": "E2", "kind": "appreciation", "sentiment": "positive", "source": "outlook", "date": "2026-08-10",
         "title": "Thanks from Treasury", "context": "Cleared the payment backlog.", "weight": 1},
        {"ref": "E3", "kind": "escalation", "sentiment": "negative", "source": "teams", "date": "2026-07-02",
         "title": "Vendor hold escalated", "context": "Released late.", "weight": 2}],
    "skills": [{"skill": "Skill A", "current": 3, "target": 3, "critical": True, "validated": True},
               {"skill": "Skill B", "current": 2, "target": 3}],
    "skills_achieved": [{"skill": "Skill A", "level": 3, "previous": 2, "date": "2026-09-20"}],
    "learning": {"completed": ["Skill A exception clinic"], "booked": 2, "actions_done": 3, "actions_open": 1, "actions_overdue": 0,
                 "curve": {"today_avg": 2.5, "projected_avg": 3.0, "target_avg": 3.0, "weeks": 3.5}},
    "open_actions": ["Shadow the AP lead on supplier calls"],
}
GOOD = {"headline": "A strong period with one escalation.", "overall": "Delivery improved; one supplier escalation.",
        "highlights": [{"text": "Cut duplicate-payment exceptions", "refs": ["E1"]}, {"text": "Treasury thanks", "refs": ["E2"]}],
        "concerns": [{"text": "A vendor hold was escalated", "refs": ["E3"]}], "evidence_mix": "Manager notes, email and Teams.",
        "skills_growth": ["Skill A certified at L3"], "learning": ["Completed the exception clinic"],
        "development_focus": ["Skill B to L3"], "talking_points": [], "suggested_rating": 3, "promotion_view": None}


def test_a9_verified_summary_keeps_its_refs(monkeypatch):
    calls = _replies(monkeypatch, a9, GOOD)
    result = a9.build_summary(RECORD)
    assert result["generated_by"] == "agent" and result["llm_calls"] == 1
    assert result["highlights"][0]["refs"] == ["E1"] and result["suggested_rating"] == 3
    assert "UNTRUSTED_DATA label=employee_record" in calls[0] and "[E3]" in calls[0] and "source teams" in calls[0]


def test_a9_rejects_invented_refs_out_of_range_rating_and_banned_language(monkeypatch):
    bad = {**GOOD, "highlights": [{"text": "Great work", "refs": ["E9"]}], "suggested_rating": 1,
           "overall": "Consider dismissal if this continues."}
    failures = a9.verify(bad, a9.normalise_payload(RECORD))["failures"]
    assert any("unknown refs" in f for f in failures) and any("suggested_rating" in f for f in failures)
    assert any("employment-decision" in f for f in failures)
    uncited = {**GOOD, "concerns": [{"text": "Something", "refs": []}]}
    assert any("must cite" in f for f in a9.verify(uncited, a9.normalise_payload(RECORD))["failures"])
    calls = _replies(monkeypatch, a9, bad, bad)
    result = a9.build_summary(RECORD)
    assert len(calls) == 2 and "failed these checks" in calls[1] and result["generated_by"] == "template"
    refs = {r for item in result["highlights"] + result["concerns"] for r in item["refs"]}
    assert refs <= {"E1", "E2", "E3"} and result["suggested_rating"] in (3, 4)


def test_a9_promotion_view_must_repeat_the_rules(monkeypatch):
    record = {**RECORD, "review_type": "promotion",
              "promotion": {"target_role": "Data Analyst", "level": "Ready in 6-12 months", "skills_band": "Developing", "pct": 60,
                            "critical_missed": ["Python Programming"], "cross_department": True}}
    mine = {**GOOD, "suggested_rating": None, "promotion_view": {"recommendation": "Ready now", "rationale": "Clearly ready."}}
    _replies(monkeypatch, a9, mine, mine)
    result = a9.build_summary(record)
    assert result["generated_by"] == "template" and result["promotion_view"]["recommendation"] == "Ready in 6-12 months"
    ok = {**mine, "promotion_view": {"recommendation": "Ready in 6-12 months", "rationale": "Python is the gap."}}
    _replies(monkeypatch, a9, ok)
    assert a9.build_summary(record)["promotion_view"] == {"recommendation": "Ready in 6-12 months", "rationale": "Python is the gap."}
    with pytest.raises(a9.SummaryInputError):
        a9.build_summary({**RECORD, "review_type": "promotion"})  # no rules readiness to repeat


def test_a9_one_on_one_needs_talking_points_and_the_template_has_them(monkeypatch):
    record = {**RECORD, "review_type": "one_on_one"}
    assert any("talking points" in f for f in a9.verify(GOOD, a9.normalise_payload(record))["failures"])
    _replies(monkeypatch, a9, RuntimeError("down"))
    result = a9.build_summary(record)
    assert result["generated_by"] == "template" and len(result["talking_points"]) >= 2
    assert result["talking_points"][0].startswith("Progress on: Shadow the AP lead")


def test_a9_rejects_unknown_review_type():
    with pytest.raises(a9.SummaryInputError):
        a9.build_summary({**RECORD, "review_type": "weekly"})


CV = ("Accounts payable analyst at a shared-services centre. Built Python scripts with pandas to automate invoice matching. "
      "Wrote SQL queries to reconcile vendor balances. Presented ageing trends to the finance director.")


def test_cv_reader_needs_verbatim_quotes_and_listed_skills(monkeypatch):
    good = {"claims": [{"skill": "python programming", "level": 3, "quote": "Built Python scripts with pandas to automate invoice matching"}]}
    _replies(monkeypatch, a8, good)
    result = a8.assess_resume(CV, ["Python Programming", "SQL & Data Wrangling"])
    assert result["generated_by"] == "agent" and result["claims"] == [
        {"skill": "Python Programming", "level": 3, "quote": "Built Python scripts with pandas to automate invoice matching"}]
    invented = {"claims": [{"skill": "Python Programming", "level": 4, "quote": "Led a team of ten data scientists"},
                           {"skill": "Machine Learning", "level": 3, "quote": "Wrote SQL queries"}]}
    failures = a8.verify_resume(invented, CV, ["Python Programming", "SQL & Data Wrangling"])["failures"]
    assert any("copied exactly" in f for f in failures) and any("not one of the listed" in f for f in failures)
    _replies(monkeypatch, a8, invented, invented)
    result = a8.assess_resume(CV, ["Python Programming", "SQL & Data Wrangling", "Machine Learning"])
    assert result["generated_by"] == "rules"
    assert {c["skill"] for c in result["claims"]} == {"Python Programming", "SQL & Data Wrangling"}
    assert all(c["level"] == 2 and c["quote"] in CV for c in result["claims"])
    assert CV not in json.dumps(result)  # the CV text itself never comes back
    with pytest.raises(a8.SkillAdviceInputError):
        a8.assess_resume("too short", ["Python Programming"])


def test_a7_reads_a_manager_note(monkeypatch):
    _replies(monkeypatch, a7, {"kind": "achievement", "sentiment": "positive", "summary": "The employee automated the ageing pack.",
                               "skills": [], "action_items": [], "confidence": 0.8})
    result = a7.classify("Automated the AP ageing pack\nRemoved four hours of manual work each week.", [], "manual")
    assert result["generated_by"] == "agent" and result["kind"] == "achievement"


class TestSummaryAndResumeEndpoints:
    def test_summary_requires_token_and_validates(self, client, monkeypatch):
        from app import main
        assert client.post("/api/performance-summary", json=RECORD).status_code == 401
        assert client.post("/api/performance-summary", json={**RECORD, "review_type": "weekly"}, headers=AUTH).status_code == 422
        assert client.post("/api/performance-summary", json={**RECORD, "band": "Fantastic"}, headers=AUTH).status_code == 422
        monkeypatch.setattr(main, "build_summary", lambda p: {"headline": "h", "generated_by": "template"})
        r = client.post("/api/performance-summary", json=RECORD, headers=AUTH)
        assert r.status_code == 200 and r.json()["ai_generated"] is True and r.json()["human_review_required"] is True

    def test_resume_skills_requires_token_validates_and_returns_only_claims(self, client, monkeypatch):
        from app import main
        files = {"file": ("cv.txt", CV.encode(), "text/plain")}
        assert client.post("/api/resume-skills", files=files, data={"skills": '["Python Programming"]'}).status_code == 401
        assert client.post("/api/resume-skills", files=files, data={"skills": "not json"}, headers=AUTH).status_code == 422
        assert client.post("/api/resume-skills", files=files, data={"skills": "[]"}, headers=AUTH).status_code == 422
        bad = {"file": ("cv.exe", b"MZ\x90\x00", "application/octet-stream")}
        assert client.post("/api/resume-skills", files=bad, data={"skills": '["Python"]'}, headers=AUTH).status_code == 400
        _replies(monkeypatch, a8, RuntimeError("down"))
        monkeypatch.setattr(main, "assess_resume", a8.assess_resume)
        r = client.post("/api/resume-skills", files=files, data={"skills": '["Python Programming"]'}, headers=AUTH)
        body = r.json()
        assert r.status_code == 200 and body["claims"][0]["skill"] == "Python Programming" and CV not in json.dumps(body)
