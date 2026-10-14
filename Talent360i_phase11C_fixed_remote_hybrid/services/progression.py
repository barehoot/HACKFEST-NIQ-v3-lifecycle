"""Skill progression after an assessment: gap analysis, level certification,
the training that follows, and vertical-promotion readiness.

Manager decision on a submitted assessment (the manager always decides; the
outcome below is only a recommendation):

  approved   -> the manager certifies the level the assessment tested for the
                skills they tick; those levels become the employee's validated
                current level (services/tni.py applies them), and Agent 5 outlines
                skill-up training towards the next level (the next role's target
                where there is one). Skills the assessment showed were still short
                are recorded as confirmed gaps and get gap sessions in the same outline.
  rejected   -> nothing is certified; the level the assessment showed for each weak
                skill is recorded as a confirmed gap (the TNI gap analysis uses it),
                and Agent 5 outlines skill-gap training from that level to the level
                tested. When every session is done, a re-assessment on the gap
                skills is auto-assigned (question_bank.baseline_need).
  sent_back  -> no level change and no training; a retake is auto-assigned
                (e.g. suspected AI use, or not enough evidence).

The outline's sessions are booked on the employee's calendar. A leader who
reopens a decision for re-review (calibration) rolls all of this back first.

Vertical promotion: each role's next role is the next grade up in the same
tower (role_ladder). Readiness compares the employee's levels -- validated
where certified -- with the next role's targets. A manager can request a
promotion-readiness assessment (built from SME-approved bank questions for
the next role) and send the employee's profile to a leader as a promotion
nomination; the leader approves or declines it. The platform records the
decision for HR; it does not change anyone's job record.
"""
from __future__ import annotations

import re
from datetime import date

from services import api_client, db, integrity, question_bank, schedule, tni
from services.backend_api import BackendAPIError

PASS_PCT = 70           # per-skill MCQ score that counts as demonstrated
READY_PCT = 80          # share of next-role skills at target for "Ready"
DEVELOPING_PCT = 50
MAX_OUTLINE_SKILLS = 6
PROMOTION_QUESTIONS = 8
SESSION_GAP_DAYS = 2    # business days between booked sessions
DECISIONS = ("approved", "rejected", "sent_back")
DECISION_LABELS = {"approved": "Approved · level certified", "rejected": "Rejected · skill-gap training",
                   "sent_back": "Sent back to retake"}
LEVEL_NAMES = ["Not assessed", "Awareness", "Working Knowledge", "Proficient", "Advanced", "Expert / Strategic"]
_GOOD_EVIDENCE = ("strong_evidence", "partial_evidence")
# Agent 5's provisional MCQ bands (agents/agent5_devplan.py _PROVISIONAL_BANDS): pct -> levels short.
_BANDS = ((85, 0), (55, 1), (25, 2))


def decision_label(decision: str | None) -> str:
    return DECISION_LABELS.get(decision or "", (decision or "").replace("_", " ").title())


# ---------------------------------------------------------------------------
# Role ladder (vertical promotion path)
# ---------------------------------------------------------------------------

def _grade_rank(grade: str | None) -> int | None:
    """B3 < B4 < P3 < P4. A multi-grade role ("B3/B4/P3") ranks at its entry grade."""
    ranks = [int(n) + (2 if band == "P" else 0) for band, n in re.findall(r"([BP])(\d)", grade or "")]
    return min(ranks) if ranks else None


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9&]{2,}", (text or "").lower()))


def role_ladder() -> dict[str, dict | None]:
    """role_id -> the next role up (same tower, next grade), or None at the top."""
    roles = db.role_master_rows()
    ladder = {}
    for role in roles:
        rank = _grade_rank(role["role_grade"])
        higher = [r for r in roles if r["tower"] == role["tower"] and rank is not None
                  and (_grade_rank(r["role_grade"]) or 0) > rank]
        if not higher:
            ladder[role["role_id"]] = None
            continue
        step = min(_grade_rank(r["role_grade"]) for r in higher)
        options = [r for r in higher if _grade_rank(r["role_grade"]) == step]
        options.sort(key=lambda r: (r["process_group"] != role["process_group"],
                                    -len(_words(r["role_name"]) & _words(role["role_name"])), r["role_id"]))
        ladder[role["role_id"]] = options[0]
    return ladder


def next_role(role_id: str | None) -> dict | None:
    return role_ladder().get(role_id) if role_id else None


def assessment_kind(cycle: dict, current: dict | None) -> dict:
    """Is this an evaluation for the role the person holds, a promotion-readiness check, or another role?"""
    role_id = cycle.get("matched_role_id")
    if current and role_id == current["role_id"]:
        return {"kind": "current_role", "label": "Current-role evaluation"}
    nxt = next_role(current["role_id"]) if current else None
    if nxt and role_id == nxt["role_id"]:
        return {"kind": "promotion", "label": f"Promotion readiness · {nxt['role_name']}"}
    return {"kind": "other_role", "label": "Other-role assessment"}


# ---------------------------------------------------------------------------
# Assessment outcome (recommendation shown to the manager)
# ---------------------------------------------------------------------------

def _shown_level(tested: int, pct: float | None, verdict: str | None) -> int:
    if pct is not None:
        short = next((width for threshold, width in _BANDS if pct >= threshold), 3)
        short = max(short, 1)  # not demonstrated -> at least one level short
    else:
        short = 1 if verdict == "partial_evidence" else 2
    return max(tested - short, 0)


def assessment_outcome(cycle: dict) -> dict:
    score = cycle.get("mcq_score") or {}
    evidence = cycle.get("evidence_report") or {}
    by_skill = {s["skill"]: s for s in score.get("by_skill", [])}
    ev_by_skill = {r["skill"]: r for r in evidence.get("evidence_results", [])}
    levels = db.cycle_question_levels(cycle["cycle_id"])
    plan = tni.employee_tni(cycle["user_id"]) or {"skills": []}
    current = {s["skill"].strip().lower(): s for s in plan["skills"]}
    proctoring = integrity.summary(cycle["cycle_id"])
    proctor_risk = proctoring["risk"] if proctoring and proctoring["proctored"] else None
    mcq_points = integrity.MEDIUM_MCQ_PENALTY if proctor_risk == "medium" else 0

    rows = []
    for skill in list(dict.fromkeys([*by_skill, *ev_by_skill])):
        mcq, ev = by_skill.get(skill), ev_by_skill.get(skill)
        tested = int(levels.get(skill) or (mcq or {}).get("target_proficiency_level")
                     or (ev or {}).get("target_proficiency_level") or 3)
        pct = mcq["pct"] if mcq and mcq.get("total") else None
        judged = max(pct - mcq_points, 0) if pct is not None else None  # medium integrity risk: points off
        verdict = ev.get("verdict") if ev else None
        ai_use = ((ev or {}).get("integrity") or {}).get("risk")
        demonstrated = ((judged is None or judged >= PASS_PCT) and (verdict is None or verdict in _GOOD_EVIDENCE)
                        and (pct is not None or verdict is not None) and ai_use != "high" and proctor_risk != "high")
        mine = current.get(skill.strip().lower())
        previous = mine["current"] if mine else None
        rows.append({
            "skill": skill,
            "is_critical": bool((mcq or {}).get("is_critical") or (ev or {}).get("is_critical")),
            "tested_level": tested, "tested_level_name": LEVEL_NAMES[tested] if 0 <= tested <= 5 else "",
            "mcq_pct": pct, "mcq_detail": f"{mcq['correct']}/{mcq['total']}" if mcq and mcq.get("total") else None,
            "verdict": verdict, "integrity": ai_use, "mcq_judged": judged if mcq_points else None,
            "demonstrated": demonstrated,
            "shown_level": tested if demonstrated else _shown_level(tested, judged, verdict),
            "previous_level": previous,
            "validated": bool(mine and mine.get("validated")),
            "missing": (ev or {}).get("missing_element") or "",
        })

    shown = [r for r in rows if r["demonstrated"]]
    critical_missed = [r["skill"] for r in rows if r["is_critical"] and not r["demonstrated"]]
    share = round(100 * len(shown) / len(rows)) if rows else 0
    overall = score.get("overall_score_pct")
    strong = bool(rows) and share >= PASS_PCT and not critical_missed and (overall is None or overall >= PASS_PCT)
    reasons = [f"{len(shown)} of {len(rows)} skills demonstrated at the tested level"]
    if overall is not None:
        reasons.append(f"MCQ {overall}%")
    if evidence.get("evidence_score_pct") is not None:
        reasons.append(f"simulation evidence {evidence['evidence_score_pct']}%")
    if critical_missed:
        reasons.append(f"critical skill(s) not shown: {', '.join(critical_missed)}")
    if evidence.get("integrity_risk") in ("medium", "high"):
        reasons.append(f"possible AI-assisted answers ({evidence['integrity_risk']} risk)")
    recommendation = "approved" if strong else "rejected"
    if proctor_risk == "high":
        recommendation = "sent_back"
        reasons.insert(0, "assessment-integrity breach: " + "; ".join(proctoring["reasons"]))
    elif proctor_risk == "medium":
        reasons.append(f"integrity risk medium: {mcq_points} points off each MCQ skill score")
    return {
        "rows": rows, "demonstrated": len(shown), "total": len(rows), "share_pct": share,
        "critical_missed": critical_missed, "band": "strong" if strong and proctor_risk != "high" else "low",
        "recommendation": recommendation, "reasons": reasons,
        "integrity_risk": evidence.get("integrity_risk"), "proctoring": proctoring, "proctor_risk": proctor_risk,
    }


# ---------------------------------------------------------------------------
# Manager decision
# ---------------------------------------------------------------------------

class DecisionError(ValueError):
    pass


def _outline_items(outcome: dict, decision: str, certified: set[str], role_id: str) -> tuple[str, list[dict]]:
    gaps = [r for r in outcome["rows"] if not r["demonstrated"] and r["skill"] not in certified]
    if decision == "rejected" and not gaps:
        gaps = outcome["rows"]  # the manager saw gaps the scores didn't: train on everything tested
    items = [{"skill": r["skill"], "from_level": r["shown_level"], "to_level": max(r["tested_level"], 1),
              "evidence_note": r["missing"][:300] or None, "is_critical": r["is_critical"], "kind": "gap"}
             for r in sorted(gaps, key=lambda r: (not r["is_critical"], r["shown_level"] - r["tested_level"]))]
    if decision == "approved":
        nxt = next_role(role_id)
        targets = {s["skill"].strip().lower(): s["target_level"] for s in db.role_skills(nxt["role_id"])} if nxt else {}
        for r in outcome["rows"]:
            if r["skill"] not in certified:
                continue
            level = max(r["tested_level"], r["previous_level"] or 0)
            goal = targets.get(r["skill"].strip().lower())
            goal = goal if goal and goal > level else level + 1
            if goal <= 5:
                items.append({"skill": r["skill"], "from_level": level, "to_level": goal, "evidence_note": None,
                              "is_critical": r["is_critical"], "kind": "skill_up"})
    return ("gap" if decision == "rejected" else "skill_up"), items[:MAX_OUTLINE_SKILLS]


def decide(cycle_id: str, decision: str, notes: str, manager_id: str, certify: list[str] | None = None,
           today: date | None = None) -> dict:
    """Records the manager's decision and everything that follows from it."""
    if decision not in DECISIONS:
        raise DecisionError("Choose approve, reject or send back.")
    cycle = db.get_cycle(cycle_id)
    if not cycle or cycle["status"] != "pending_manager_approval":
        raise DecisionError("This assessment is not waiting for a decision.")
    outcome = assessment_outcome(cycle)
    chosen = {r["skill"] for r in outcome["rows"] if r["skill"] in set(certify or [])}
    if decision == "approved" and not chosen:
        raise DecisionError("Tick at least one skill to certify, or reject / send back instead.")
    if decision != outcome["recommendation"] and decision != "sent_back" and len(notes.strip()) < 10:
        raise DecisionError("Your decision differs from the recommended outcome, so add a short rationale for the record.")
    if decision == "approved" and outcome["proctor_risk"] == "high" and len(notes.strip()) < integrity.OVERRIDE_MIN_CHARS:
        raise DecisionError(f"This attempt has a high-risk integrity record. Certifying it overrides the "
                            f"recommendation: explain why in at least {integrity.OVERRIDE_MIN_CHARS} characters.")
    if decision != "sent_back" and api_client.evidence_pending(cycle) and len(notes.strip()) < 10:
        raise DecisionError("The AI evidence report isn't ready yet, so add a short rationale from what you read in "
                            "the conversation, or wait for the report.")

    if not api_client.decide_manager_cycle(cycle_id, decision, notes, manager_id):
        raise DecisionError("This assessment is not waiting for a decision.")

    result = {"decision": decision, "certified": 0, "gaps": 0, "outline_id": None, "sessions": 0, "outline_ready": False,
              "hr_alerted": outcome["proctor_risk"] == "high" and integrity.report_repeat(cycle, manager_id)}
    record = {"user_id": cycle["user_id"], "role_id": cycle["matched_role_id"], "cycle_id": cycle_id, "certified_by": manager_id}
    if decision == "approved":
        by_skill = {r["skill"]: r for r in outcome["rows"]}
        result["certified"] = db.add_certifications(
            **record, levels=[(s, by_skill[s]["tested_level"], by_skill[s]["previous_level"]) for s in sorted(chosen)
                              if by_skill[s]["tested_level"] >= 1])
    if decision != "sent_back":
        # The manager has reviewed the evidence: the weak skills' shown levels are confirmed gaps.
        result["gaps"] = db.add_certifications(
            **record, basis="assessed",
            levels=[(r["skill"], r["shown_level"], r["previous_level"]) for r in outcome["rows"]
                    if not r["demonstrated"] and r["skill"] not in chosen])
    if decision != "sent_back":
        mode, items = _outline_items(outcome, decision, chosen, cycle["matched_role_id"])
        if items:
            outline_id = db.create_outline(cycle_id=cycle_id, user_id=cycle["user_id"], mode=mode, items=items,
                                           created_by=manager_id)
            outline = prepare_outline(outline_id, today=today)
            result.update(outline_id=outline_id, outline_ready=bool(outline and outline["outline"]),
                          sessions=len(((outline or {}).get("outline") or {}).get("sessions", [])))
    return result


# ---------------------------------------------------------------------------
# Training outline (Agent 5) and its calendar sessions
# ---------------------------------------------------------------------------

def _get_outline(outline_id: str) -> dict | None:
    rows = [o for o in db.list_outlines(include_superseded=True) if o["outline_id"] == outline_id]
    return rows[0] if rows else None


def prepare_outline(outline_id: str, today: date | None = None) -> dict | None:
    """Generates (once) the outline with Agent 5 and books its sessions. If the
    pipeline API is unreachable the outline stays pending and is retried the
    next time anyone opens it -- the manager's decision is already recorded."""
    outline = _get_outline(outline_id)
    if not outline or outline["status"] != "active":
        return outline
    if not outline["outline"]:
        cycle = db.get_cycle(outline["cycle_id"])
        items = [{k: i[k] for k in ("skill", "from_level", "to_level", "evidence_note")} for i in outline["items"]]
        try:
            content = api_client.backend_training_outline(cycle["matched_role_id"], outline["mode"], items)
        except BackendAPIError:
            return outline
        db.set_outline_content(outline_id, outline=content)
        outline["outline"] = content
    if outline["event_ids"] is None:
        outline["event_ids"] = _book_sessions(outline, today or date.today())
        db.set_outline_content(outline_id, event_ids=outline["event_ids"])
    return outline


def _book_sessions(outline: dict, today: date) -> list[str]:
    cycle = db.get_cycle(outline["cycle_id"])
    start = schedule.add_business_days(today, 1)
    last = db.last_training_date(outline["user_id"])
    if last and date.fromisoformat(last) >= start:
        start = schedule.add_business_days(date.fromisoformat(last), 1)
    label = "Skill-gap training" if outline["mode"] == "gap" else "Skill-up training"
    ids = []
    for n, session in enumerate(outline["outline"].get("sessions", [])):
        day = schedule.add_business_days(start, n * SESSION_GAP_DAYS)
        ids.append(db.create_event(
            event_type="training", title=f"{label}: {session['title']}", event_date=day.isoformat(),
            start_time="10:00", duration_minutes=session["duration_minutes"], user_id=outline["user_id"],
            role_id=cycle["matched_role_id"], skill=session["skill"], course_id=session.get("course_id"),
            notes=(f"Session {session['session_number']} of {len(outline['outline']['sessions'])} · "
                   f"L{session['from_level'] if session['from_level'] is not None else '?'}→L{session['to_level']} · "
                   f"from the manager's decision on {outline['cycle_id']}"),
            created_by=outline["created_by"],
        ))
    return ids


def outline_view(outline: dict, today: date | None = None) -> dict:
    """An outline with each session's booking and progress, for the employee and manager pages."""
    outline = prepare_outline(outline["outline_id"], today) or outline
    event_ids = outline.get("event_ids") or []
    sessions = []
    for n, session in enumerate((outline.get("outline") or {}).get("sessions", [])):
        sessions.append({**session, "event": db.get_event(event_ids[n]) if n < len(event_ids) else None})
    booked = [s for s in sessions if s["event"] and s["event"]["status"] != "cancelled"]
    done = sum(1 for s in booked if s["event"]["status"] == "completed")
    cycle = db.get_cycle(outline["cycle_id"]) or {}
    return {**outline, "sessions": sessions, "employee_name": (db.get_user(outline["user_id"]) or {}).get("full_name"), "done": done, "booked": len(booked),
            "complete": bool(booked) and done == len(booked),
            "pending": not outline.get("outline"),
            "role_name": cycle.get("matched_role_name"), "decision": cycle.get("manager_decision"),
            "decided_at": (cycle.get("updated_at") or "")[:10],
            "template": (outline.get("outline") or {}).get("generated_by") == "template"}


def outlines_for(user_ids: list[str], today: date | None = None) -> list[dict]:
    rows = []
    for user_id in user_ids:
        rows += [outline_view(o, today) for o in db.list_outlines(user_id=user_id)]
    return rows


def reopen_for_rereview(cycle_id: str, today: date) -> dict:
    """A leader sent the decision back to the manager: undo what it set in motion."""
    revoked = db.revoke_certifications(cycle_id)
    cancelled = 0
    for outline in db.supersede_outlines(cycle_id):
        for event_id in outline.get("event_ids") or []:
            event = db.get_event(event_id)
            if event and event["status"] == "scheduled" and db.set_event_status(event_id, "cancelled"):
                cancelled += 1
    return {"revoked": revoked, "cancelled": cancelled}


# ---------------------------------------------------------------------------
# Promotion readiness and nominations
# ---------------------------------------------------------------------------

def _levels_from(plan: dict | None, certifications: list[dict]) -> dict[str, dict]:
    """skill -> {"level", "validated"} from a TNI view plus the person's manager-reviewed
    records (newest first)."""
    out = {}
    for s in (plan or {}).get("skills", []):
        if s["assessed"]:
            out[s["skill"].strip().lower()] = {"level": s["current"], "validated": bool(s["validated"])}
    current_role_skills = {s["skill"].strip().lower() for s in (plan or {}).get("skills", [])}
    seen = set()
    for record in certifications:
        key = record["skill"].strip().lower()
        if key in seen or key in current_role_skills:  # current-role skills: TNI already applied them
            continue
        seen.add(key)  # e.g. a skill only the promotion assessment covered
        out[key] = {"level": record["level"], "validated": record["basis"] == "certified"}
    return out


def _levels(user_id: str) -> dict[str, dict]:
    """skill -> {"level", "validated"} from the TNI view (certified levels already applied)."""
    return _levels_from(tni.employee_tni(user_id), db.list_certifications([user_id], basis=None))


def role_by_id(role_id: str | None) -> dict | None:
    return next((r for r in db.role_master_rows() if r["role_id"] == role_id), None) if role_id else None


def readiness(user_id: str, role: dict | None = None, target_role_id: str | None = None) -> dict:
    """Readiness for the next role up the ladder, or for any target role (another department or
    vertical) when target_role_id is given. Only assessed / validated levels count here."""
    role = role or tni.current_role(user_id)
    nxt = role_by_id(target_role_id) if target_role_id else (next_role(role["role_id"]) if role else None)
    if not nxt:
        return readiness_from(user_id, role, None, {}, [])
    return readiness_from(user_id, role, nxt, _levels(user_id), db.role_skill_details(nxt["role_id"]))


def readiness_from(user_id: str, role: dict | None, nxt: dict | None, levels: dict[str, dict],
                   next_skills: list[dict]) -> dict:
    """Readiness for the next role from preloaded data (the status engine scores people in bulk)."""
    view = {"user_id": user_id, "current": role, "next": nxt, "rows": [], "pct": None, "band": None,
            "critical_missed": [], "validated": 0, "unknown": 0}
    if not nxt:
        return view
    for s in next_skills:
        mine = levels.get(s["skill"].strip().lower())
        level = mine["level"] if mine else None
        view["rows"].append({"skill": s["skill"], "target": s["target_level"], "is_critical": bool(s["is_critical"]),
                             "level": level, "validated": bool(mine and mine["validated"]),
                             "met": level is not None and level >= s["target_level"],
                             "gap": max(s["target_level"] - level, 0) if level is not None else None})
    rows = view["rows"]
    met = sum(1 for r in rows if r["met"])
    view.update(pct=round(100 * met / len(rows)) if rows else 0, met=met, total=len(rows),
                critical_missed=[r["skill"] for r in rows if r["is_critical"] and not r["met"]],
                validated=sum(1 for r in rows if r["validated"]), unknown=sum(1 for r in rows if r["level"] is None))
    view["band"] = ("Ready" if view["pct"] >= READY_PCT and not view["critical_missed"] else
                    "Developing" if view["pct"] >= DEVELOPING_PCT else "Not yet")
    return view


def _history(user_id: str) -> list[dict]:
    out = []
    for c in db.list_cycles_for_user(user_id):
        evidence = c.get("evidence_report") or {}
        out.append({
            "cycle_id": c["cycle_id"], "role_name": c["matched_role_name"], "status": c["status"],
            "decision": c.get("manager_decision"), "decision_label": decision_label(c.get("manager_decision")),
            "mcq_pct": (c.get("mcq_score") or {}).get("overall_score_pct"),
            "evidence_pct": evidence.get("evidence_score_pct"), "integrity_risk": evidence.get("integrity_risk"),
            "date": (c["updated_at"] or "")[:10], "source": question_bank.SOURCE_LABELS.get(c["match_status"], "Assessment"),
        })
    return out


def team_promotions(manager_id: str | None) -> list[dict]:
    """One row per employee (a manager's reports, or everyone for a leader) with promotion readiness."""
    employees = [e for e in db.list_employees() if manager_id is None or e["manager_id"] == manager_id]
    roles = tni.current_roles(employees)
    nominations = {}
    for n in db.list_nominations(user_ids=[e["user_id"] for e in employees]):
        nominations.setdefault(n["user_id"], n)  # pending first, then newest
    bank = {}
    for q in db.list_bank(status="approved"):
        bank[q["role_id"]] = bank.get(q["role_id"], 0) + 1
    rows = []
    for e in employees:
        r = readiness(e["user_id"], roles.get(e["user_id"]))
        cycles = db.list_cycles_for_user(e["user_id"])
        active = next((c for c in cycles if c["status"] in question_bank.ACTIVE_STATUSES), None)
        promo = next((c for c in cycles if r["next"] and c["matched_role_id"] == r["next"]["role_id"]), None)
        decided = next((c for c in cycles if c.get("manager_decision") and c["status"] in ("manager_approved", "manager_rejected")), None)
        rows.append({
            "user_id": e["user_id"], "name": e["full_name"], "manager_id": e["manager_id"], **r,
            "active": active, "promotion_cycle": promo, "last_decided": decided,
            "evaluated": any(c["status"] == "manager_approved" for c in cycles),
            "nomination": nominations.get(e["user_id"]),
            "bank_for_next": bank.get(r["next"]["role_id"], 0) if r["next"] else 0,
        })
    order = {"Ready": 0, "Developing": 1, "Not yet": 2, None: 3}
    return sorted(rows, key=lambda r: (order[r["band"]], -(r["pct"] or 0), r["name"]))


def request_promotion_assessment(user_id: str, requested_by: str, target_role_id: str | None = None) -> str:
    """Builds a promotion-readiness assessment for the next role -- or a career-aspiration role in
    another department / vertical -- from SME-approved bank questions."""
    r = readiness(user_id, target_role_id=target_role_id)
    if not r["next"]:
        raise DecisionError("This role has no next role on the ladder." if not target_role_id else "Unknown target role.")
    focus = [row["skill"] for row in r["rows"] if not row["met"]]
    selection = question_bank.select_questions(r["next"]["role_id"], PROMOTION_QUESTIONS, None, focus,
                                               avoid=db.seen_bank_questions(user_id))
    if not selection["selected"]:
        raise DecisionError(f"No SME-approved questions for {r['next']['role_name']} yet. Ask an SME to add some.")
    result = question_bank.deploy(role_id=r["next"]["role_id"], question_ids=[q["question_id"] for q in selection["selected"]],
                                  deployed_by=requested_by, employee_ids=[user_id], source="promotion")
    if not result["assigned"]:
        raise DecisionError("This employee already has an assessment in progress.")
    return result["assigned"][0][1]


def nomination_profile(user_id: str, target_role_id: str | None = None) -> dict:
    """The profile a manager sends to a leader: readiness, validated levels and assessment history."""
    user = db.get_user(user_id) or {}
    r = readiness(user_id, target_role_id=target_role_id)
    plan = tni.employee_tni(user_id)
    cross = bool(r["current"] and r["next"] and r["current"].get("tower") != r["next"].get("tower"))
    return {
        "name": user.get("full_name"), "current_role": r["current"], "next_role": r["next"], "cross_vertical": cross,
        "readiness": {k: r.get(k) for k in ("pct", "band", "met", "total", "critical_missed", "validated", "unknown")},
        "rows": r["rows"],
        "current_role_summary": plan["summary"] if plan else None,
        "certified": [{"skill": c["skill"], "level": c["level"], "date": (c["created_at"] or "")[:10],
                       "by": c.get("certified_by_name")} for c in db.list_certifications([user_id])],
        "history": _history(user_id),
        "snapshot_date": date.today().isoformat(),
    }


def nominate(user_id: str, manager_id: str, note: str, target_role_id: str | None = None) -> str:
    r = readiness(user_id, target_role_id=target_role_id)
    if not r["next"]:
        raise DecisionError("There is no next role on the ladder for this employee." if not target_role_id else "Unknown target role.")
    if not any(c["status"] == "manager_approved" for c in db.list_cycles_for_user(user_id)):
        raise DecisionError("Nominate after at least one assessment has been approved, so the profile has validated evidence.")
    if len(note.strip()) < 20:
        raise DecisionError("Explain why you are nominating this person (at least a couple of sentences).")
    nomination_id = db.create_nomination(user_id=user_id, from_role_id=(r["current"] or {}).get("role_id"),
                                         to_role_id=r["next"]["role_id"], profile=nomination_profile(user_id, target_role_id),
                                         manager_id=manager_id, manager_note=note.strip())
    if not nomination_id:
        raise DecisionError("A nomination for this employee is already with a leader.")
    return nomination_id


def decide_nomination(nomination_id: str, leader_id: str, decision: str, note: str) -> None:
    if decision not in ("approved", "declined"):
        raise DecisionError("Choose approve or decline.")
    if decision == "declined" and len(note.strip()) < 10:
        raise DecisionError("Tell the manager why (at least a sentence), so they can plan the next step.")
    if not db.decide_nomination(nomination_id, decision, leader_id, note.strip()):
        raise DecisionError("This nomination has already been decided.")


def readiness_summary(rows: list[dict]) -> dict:
    """Counts by band and tower for the leader's promotion view."""
    towers: dict[str, dict] = {}
    for r in rows:
        if not r["next"]:
            continue
        t = towers.setdefault(r["current"]["tower"], {"tower": r["current"]["tower"], "Ready": 0, "Developing": 0, "Not yet": 0})
        t[r["band"]] += 1
    with_path = [r for r in rows if r["next"]]
    return {"towers": sorted(towers.values(), key=lambda t: t["tower"]),
            "ready": sum(1 for r in with_path if r["band"] == "Ready"),
            "developing": sum(1 for r in with_path if r["band"] == "Developing"),
            "not_yet": sum(1 for r in with_path if r["band"] == "Not yet"),
            "with_path": len(with_path), "top_of_ladder": len(rows) - len(with_path),
            "ready_not_nominated": [r for r in with_path if r["band"] == "Ready" and not (r["nomination"] and r["nomination"]["status"] == "pending")]}
