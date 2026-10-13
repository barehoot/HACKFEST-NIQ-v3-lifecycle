"""Question bank: SME-approved questions reused across assessments.

- select_questions(): the selection agent. Proposes a question set for a
  role from the bank (no LLM): skills in priority order, one question per
  skill before any skill gets a second, the closest proficiency level, and
  less-used questions first so the same items don't go to everyone. The
  SME adjusts the proposal before deploying it.
- deploy(): puts the chosen questions into an employee's assessment cycle
  (a resume submission the SME is setting up, or a new SME assignment),
  optionally booking a due date on the calendar.
- ensure_baseline(): automatic assessment, built only from pre-approved bank
  questions (so no unreviewed AI content reaches the employee), for anyone
  with no assessment in progress who
    * has never been evaluated in Talent360i for the role they hold now
      (dataset levels alone don't count as an evaluation), or
    * was sent back by their manager to retake the assessment, or
    * was rejected with skill-gap training and has finished that training
      (the re-assessment targets the gap skills).
"""
from __future__ import annotations

from datetime import date

from services import db, schedule, tni

AUTO_BASELINE_QUESTIONS = 8
ACTIVE_STATUSES = {"resume_uploaded", "pending_sme_setup", "pending_sme_approval", "ready_for_assessment",
                   "mcq_submitted", "case_study_submitted", "pending_manager_approval"}
SOURCE_LABELS = {"auto_matched": "Resume submission", "sme_assigned": "Assigned by SME",
                 "auto_baseline": "Baseline (auto-assigned)", "auto_retake": "Retake (sent back by manager)",
                 "auto_reassessment": "Re-assessment after skill-gap training",
                 "promotion": "Promotion readiness (requested by manager)"}


def select_questions(role_id: str, count: int, level: int | None = None,
                     focus_skills: list[str] | None = None, avoid: set[str] | None = None) -> dict:
    """avoid: bank question ids this person has already seen -- used only once nothing fresher is left."""
    avoid = avoid or set()
    bank = db.list_bank(role_id, "approved")
    skills = db.role_skills(role_id)
    target = {s["skill"]: (level or s["target_level"]) for s in skills}
    focus = [s for s in (focus_skills or []) if s in target]
    order = focus + [s["skill"] for s in skills if s["skill"] not in focus]
    order += sorted({q["skill"] for q in bank} - set(order))

    by_skill: dict[str, list[dict]] = {}
    for q in bank:
        by_skill.setdefault(q["skill"], []).append(q)
    for skill, questions in by_skill.items():
        wanted = target.get(skill, level or 3)
        questions.sort(key=lambda q: (q["question_id"] in avoid, abs((q["target_level"] or wanted) - wanted), q["times_used"],
                                      -(q["ai_confidence"] or 0), q["question_id"]))

    critical = {s["skill"] for s in skills if s["is_critical"]}
    selected, round_index = [], 0
    while len(selected) < count and any(len(by_skill.get(s, [])) > round_index for s in order):
        for skill in order:
            if len(selected) >= count:
                break
            candidates = by_skill.get(skill, [])
            if len(candidates) > round_index:
                q = candidates[round_index]
                reasons = ["unassessed skill" if skill in focus else "critical skill" if skill in critical else "role skill"]
                wanted = target.get(skill)
                if wanted and q["target_level"] == wanted:
                    reasons.append(f"level match L{wanted}")
                elif wanted:
                    reasons.append(f"closest level L{q['target_level']} (wanted L{wanted})")
                reasons.append(f"used {q['times_used']}×" + (" · seen before" if q["question_id"] in avoid else ""))
                selected.append({**q, "reason": " · ".join(reasons)})
        round_index += 1

    return {
        "selected": selected,
        "others": [q for q in bank if q["question_id"] not in {s["question_id"] for s in selected}],
        "available": len(bank),
        # Role skills the bank can't cover at all (priority skills first) -- what to generate next.
        "uncovered": [skill for skill in order if skill in target and skill not in by_skill],
        "skills": skills,
    }


def _active_cycle(user_id: str) -> dict | None:
    return next((c for c in db.list_cycles_for_user(user_id) if c["status"] in ACTIVE_STATUSES), None)


def deploy(*, role_id: str, question_ids: list[str], deployed_by: str | None, employee_ids: list[str] = (),
           cycle_id: str | None = None, due_date: date | None = None, source: str = "sme_assigned") -> dict:
    """Deploys approved bank questions. Returns {"assigned": [(user_id, cycle_id)], "skipped": [(user_id, reason)]}."""
    bank = {q["question_id"]: q for q in db.list_bank(role_id, "approved")}
    chosen = [bank[qid] for qid in dict.fromkeys(question_ids) if qid in bank]
    if not chosen:
        raise ValueError("Select at least one approved question for this role.")
    role = next((r for r in db.list_roles() if r["role_id"] == role_id), None)
    if not role:
        raise ValueError("Unknown role.")

    targets: list[tuple[str, str | None]] = []
    skipped: list[tuple[str, str]] = []
    if cycle_id:
        cycle = db.get_cycle(cycle_id)
        if not cycle or cycle["status"] != "pending_sme_setup":
            raise ValueError("That resume submission is no longer waiting for set-up.")
        targets.append((cycle["user_id"], cycle_id))
    for user_id in dict.fromkeys(employee_ids):
        user = db.get_user(user_id)
        if not user or user["role"] != "employee":
            skipped.append((user_id, "not an employee"))
        elif _active_cycle(user_id):
            skipped.append((user_id, "already has an assessment in progress"))
        else:
            targets.append((user_id, None))

    assigned = []
    for user_id, existing in targets:
        cid = existing or db.create_cycle(
            user_id=user_id, resume_filename=None, resume_text=None, matched_role_id=role_id,
            matched_role_name=role["role_name"], matched_role_grade=role["role_grade"], match_status=source,
            status="ready_for_assessment",
        )
        db.attach_bank_questions(cid, chosen, deployed_by)
        if existing:
            db.update_cycle(cid, status="ready_for_assessment")
        if due_date:
            schedule.book_assessment(user_id=user_id, event_date=due_date, start_time=None, duration=max(10, 2 * len(chosen)),
                                     created_by=deployed_by or "system", title=f"Skill assessment · {role['role_name']}")
        assigned.append((user_id, cid))
    return {"assigned": assigned, "skipped": skipped}


def _remediation_complete(cycle_id: str) -> bool | None:
    """None: no training outline; False: training booked/being prepared; True: every session done."""
    outlines = db.list_outlines(cycle_id=cycle_id)
    if not outlines:
        return None
    events = [db.get_event(e) for e in (outlines[0].get("event_ids") or [])]
    events = [e for e in events if e]
    if not events:
        return False
    return all(e["status"] in ("completed", "cancelled") for e in events) and any(e["status"] == "completed" for e in events)


def _gap_skills(cycle_id: str) -> list[str]:
    outlines = db.list_outlines(cycle_id=cycle_id)
    return [i["skill"] for i in (outlines[0]["items"] if outlines else [])]


def baseline_need(user_id: str, plan: dict | None = None) -> dict:
    """Whether an employee should be auto-assigned an assessment now, for which role, and why."""
    cycles = db.list_cycles_for_user(user_id)  # newest first
    if any(c["status"] in ACTIVE_STATUSES for c in cycles):
        return {"needs": False, "reason": "Has an assessment in progress"}
    role = tni.current_role(user_id)
    if not role:
        return {"needs": False, "reason": "No role on file to assess against"}
    plan = plan if plan is not None else tni.employee_tni(user_id)
    unassessed = [s["skill"] for s in plan["skills"] if not s["assessed"]] if plan and plan["role_id"] == role["role_id"] else []
    base = {"role_id": role["role_id"], "role_name": role["role_name"], "focus": unassessed}

    decided = [c for c in cycles if c["matched_role_id"] == role["role_id"]
               and c["status"] in ("manager_approved", "manager_rejected")]
    if not decided:
        return {**base, "needs": True, "source": "auto_baseline",
                "reason": "No skill evaluation yet for the current role"}
    last = decided[0]
    if last["status"] == "manager_approved":
        return {"needs": False, "reason": "Evaluated for the current role"}
    if last["manager_decision"] == "sent_back":
        return {**base, "needs": True, "source": "auto_retake", "reason": "Sent back by the manager to retake"}
    done = _remediation_complete(last["cycle_id"])
    if done is False:
        return {"needs": False, "reason": "Skill-gap training in progress"}
    gaps = _gap_skills(last["cycle_id"])
    return {**base, "needs": True, "source": "auto_reassessment", "focus": gaps + [s for s in unassessed if s not in gaps],
            "reason": "Skill-gap training complete: re-assessment due" if done else "Rejected: re-assessment due"}


def ensure_baseline(user_id: str) -> dict:
    """Auto-assigns a pre-approved assessment if the employee needs one and the bank can cover it."""
    need = baseline_need(user_id)
    if not need["needs"]:
        return {"assigned": False, "reason": need["reason"]}
    selection = select_questions(need["role_id"], AUTO_BASELINE_QUESTIONS, None, need["focus"],
                                 avoid=db.seen_bank_questions(user_id))
    if not selection["selected"]:
        return {"assigned": False, "reason": f"No approved questions in the bank for {need['role_name']} yet"}
    result = deploy(role_id=need["role_id"], question_ids=[q["question_id"] for q in selection["selected"]],
                    deployed_by=None, employee_ids=[user_id], source=need["source"])
    return {"assigned": bool(result["assigned"]), "reason": need["reason"], "source": need["source"],
            "cycle_id": result["assigned"][0][1] if result["assigned"] else None}


def run_auto_baseline() -> dict:
    assigned, blocked = 0, {}
    for employee in db.list_employees():
        outcome = ensure_baseline(employee["user_id"])
        if outcome["assigned"]:
            assigned += 1
        elif outcome["reason"].startswith("No approved questions"):
            blocked[outcome["reason"]] = blocked.get(outcome["reason"], 0) + 1
    return {"assigned": assigned, "blocked": blocked}


def roster() -> dict:
    """Every employee with their role, TNI state, current assessment and what the SME can assign."""
    bank_counts: dict[str, int] = {}
    for q in db.list_bank(status="approved"):
        bank_counts[q["role_id"]] = bank_counts.get(q["role_id"], 0) + 1
    org = {p["user_id"]: p for p in tni.org_tni()["people"]}
    users = {u["user_id"]: u for u in db.list_users()}
    rows = []
    for employee in db.list_employees():
        uid = employee["user_id"]
        person = org.get(uid)
        cycles = db.list_cycles_for_user(uid)
        latest = cycles[0] if cycles else None
        role = ({"role_id": person["role_id"], "role_name": person["role_name"]} if person
                else db.role_by_name(employee["job_title"]) or {"role_id": None, "role_name": employee["job_title"]})
        need = baseline_need(uid, person)
        rows.append({
            "user_id": uid, "name": employee["full_name"], "role_id": role["role_id"], "role_name": role["role_name"],
            "manager": (users.get(employee["manager_id"]) or {}).get("full_name", "—"),
            "assessed": person["summary"]["assessed"] if person else 0, "total": person["summary"]["total"] if person else 0,
            "not_assessed": person["summary"]["not_assessed"] if person else None,
            "stage": latest["status"] if latest else None, "cycle_id": latest["cycle_id"] if latest else None,
            "source": SOURCE_LABELS.get(latest["match_status"], "Assessment") if latest else None,
            "active": bool(latest and latest["status"] in ACTIVE_STATUSES),
            "needs_baseline": need.get("needs", False), "need_reason": need.get("reason"),
            "need_source": SOURCE_LABELS.get(need.get("source"), "") if need.get("needs") else "",
            "bank": bank_counts.get(role["role_id"], 0),
        })
    return {"rows": rows, "setup_queue": db.cycles_in_status("pending_sme_setup"),
            "eligible": sum(1 for r in rows if r["needs_baseline"]),
            "eligible_blocked": sum(1 for r in rows if r["needs_baseline"] and not r["bank"])}
