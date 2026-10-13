"""Job profiles, verticals, departments and the skills each job requires -- kept current as the market moves.

  - Admins, HR partners and leaders create verticals (towers), departments and job
    profiles (a new role joins the role catalogue and, after the nightly re-index,
    resume matching). Onboarding a role walks through all three, then Agent 8
    pitches the skills it should require (services/skill_advisor.py).
  - Required skills change through recorded requirement changes: add a skill,
    change its target level or criticality, or remove it, always with a reason
    (optionally linked to a market trend). Admins and leaders apply a change
    directly; a manager's change is a proposal an admin or leader approves.
  - Trends (emerging / rising / stable / declining skills) are logged by managers,
    leaders and admins and shown next to the roles they affect.

The live role definition drives everything downstream: TNI targets
(services/tni.py), status scoring, promotion readiness, the SME's question
generation (Agent 2 grounds on the behaviour indicator) and the assessment
blueprint. Changing a target re-scores everyone in that role on their next
status refresh -- no data migration.
"""
from __future__ import annotations

import re

from domain.governance import sanitise_text
from services import db, engagement, lifecycle_store as store

EDITOR_ROLES = ("admin", "leader", "hr")
PROPOSER_ROLES = ("manager",)
CATALOGUE_ROLES = ("admin", "hr")  # who maintains the skill catalogue itself
DIRECTIONS = {"emerging": "Emerging", "rising": "Rising", "stable": "Stable", "declining": "Declining"}
ACTIONS = {"add": "Add skill", "update": "Change requirement", "remove": "Remove skill"}


class JobProfileError(ValueError):
    pass


def _slug(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")[:60]


def role_id_for(tower: str, name: str) -> str:
    return f"{tower}_{_slug(sanitise_text(name, 120))}"


def role_exists(tower: str, name: str) -> bool:
    """Same id, or the same job title in the same vertical."""
    role_id, title = role_id_for(tower, name), sanitise_text(name, 120).lower()
    return any(r["role_id"] == role_id or (r["tower"] == tower and (r["role_name"] or "").strip().lower() == title)
               for r in db.role_master_rows())


def towers() -> list[str]:
    """Vertical codes, live: the three dataset towers plus any vertical created since."""
    return store.vertical_codes()


def create_vertical(*, code: str, name: str, description: str, user: dict) -> str:
    if user["role"] not in EDITOR_ROLES:
        raise PermissionError("Only admins, HR partners and leaders create verticals.")
    code = sanitise_text(code, 8).upper().replace(" ", "")
    name = sanitise_text(name, 60)
    if not re.fullmatch(r"[A-Z][A-Z0-9]{1,7}", code):
        raise JobProfileError("The vertical code is 2-8 letters or digits, starting with a letter (e.g. FPA, TAX).")
    if len(name) < 3:
        raise JobProfileError("Give the vertical a name (e.g. Financial Planning & Analysis).")
    if not store.create_vertical(code=code, name=name, description=sanitise_text(description, 400), created_by=user["id"]):
        raise JobProfileError("A vertical with that code or name already exists.")
    return code


def catalogue() -> dict:
    roles = db.role_master_rows()
    meta = store.job_profiles()
    pending = store.list_skill_changes(status="proposed")
    pending_by_role: dict[str, int] = {}
    for change in pending:
        pending_by_role[change["role_id"]] = pending_by_role.get(change["role_id"], 0) + 1
    rows = []
    for role in roles:
        skills = store.role_skill_rows(role["role_id"])
        info = meta.get(role["role_id"], {})
        rows.append({**role, "skills": len(skills), "critical": sum(1 for s in skills if s["is_critical"]),
                     "department": info.get("department_name"), "dept_id": info.get("dept_id"),
                     "pending": pending_by_role.get(role["role_id"], 0), "custom": bool(info.get("created_by"))})
    towers: dict[str, list] = {}
    for row in rows:
        towers.setdefault(row["tower"] or "Other", []).append(row)
    return {"towers": towers, "roles": rows, "departments": store.list_departments(), "pending": pending,
            "trends": store.list_trends(), "recent": store.list_skill_changes(limit=15), "verticals": store.list_verticals()}


def role_detail(role_id: str) -> dict | None:
    role = next((r for r in db.role_master_rows() if r["role_id"] == role_id), None)
    if not role:
        return None
    info = store.job_profiles().get(role_id, {})
    return {"role": role, "profile": info, "skills": store.role_skill_rows(role_id),
            "changes": store.list_skill_changes(role_id=role_id), "trends": store.list_trends(role["tower"]),
            "skill_options": store.skill_master_rows(), "departments": store.list_departments(),
            "people": store.count_with_title(role["role_name"].strip())}


def create_department(*, name: str, tower: str, description: str, leader_id: str | None, user: dict) -> str:
    if user["role"] not in EDITOR_ROLES:
        raise PermissionError("Only admins, HR partners and leaders create departments.")
    name = sanitise_text(name, 80)
    if len(name) < 2:
        raise JobProfileError("Give the department a name.")
    if leader_id and (db.get_user(leader_id) or {}).get("role") != "leader":
        raise JobProfileError("Choose a leader for the department.")
    dept_id = store.create_department(name=name, tower=tower if tower in towers() else None,
                                      description=sanitise_text(description, 400), leader_id=leader_id, created_by=user["id"])
    if not dept_id:
        raise JobProfileError("A department with that name already exists.")
    return dept_id


def validate_role_fields(*, name: str, grade: str, summary: str) -> None:
    """The job fields every new profile needs; checked before anything is created."""
    if len(sanitise_text(name, 120)) < 3:
        raise JobProfileError("Give the job a name.")
    if not re.fullmatch(r"[BP]\d(/[BP]\d)*", sanitise_text(grade, 20).upper()):
        raise JobProfileError("Grade looks like B3, B4, P3 or a range such as B3/B4.")
    if len(sanitise_text(summary, 1000)) < 30:
        raise JobProfileError("Write a short role summary (30+ characters) -- resume matching uses it.")


def create_role(*, name: str, tower: str, process_group: str, grade: str, dept_id: str | None, summary: str,
                responsibilities: str, skills_text: str, user: dict) -> str:
    if user["role"] not in EDITOR_ROLES:
        raise PermissionError("Only admins, HR partners and leaders create job profiles.")
    validate_role_fields(name=name, grade=grade, summary=summary)
    name, grade = sanitise_text(name, 120), sanitise_text(grade, 20).upper()
    if tower not in towers():
        raise JobProfileError("Choose the job's vertical.")
    if dept_id and not store.get_department(dept_id):
        raise JobProfileError("Unknown department.")
    role_id = role_id_for(tower, name)
    if role_exists(tower, name) or not store.create_role(role_id=role_id, tower=tower, process_group=sanitise_text(process_group, 80) or tower,
                             role_name=name, role_grade=grade, summary=sanitise_text(summary, 1000),
                             responsibilities=sanitise_text(responsibilities, 1500), skills_text=sanitise_text(skills_text, 800)):
        raise JobProfileError("A job profile with that name already exists in this vertical.")
    store.upsert_job_profile(role_id, dept_id=dept_id or None, description=sanitise_text(summary, 1000), by=user["id"])
    return role_id


def _payload(form: dict) -> dict:
    try:
        target = int(form.get("target_level") or 0)
    except (TypeError, ValueError):
        target = 0
    return {"target_level": target, "is_critical": bool(form.get("is_critical")),
            "capability": sanitise_text(form.get("capability"), 80) or None,
            "behavior_indicator": sanitise_text(form.get("behavior_indicator"), 400) or None}


def request_change(role_id: str, *, action: str, skill: str, form: dict, reason: str, trend_id: str | None,
                   user: dict) -> dict:
    """Editors apply immediately; managers create a proposal. Returns {"change_id", "applied"}."""
    if user["role"] not in EDITOR_ROLES + PROPOSER_ROLES:
        raise PermissionError("Only managers, leaders, HR partners and admins can change job requirements.")
    detail = role_detail(role_id)
    if not detail:
        raise JobProfileError("Unknown job profile.")
    skill, reason = sanitise_text(skill, 120), sanitise_text(reason, 600)
    if action not in ACTIONS or len(skill) < 2:
        raise JobProfileError("Choose what to change and the skill.")
    if len(reason) < 10:
        raise JobProfileError("Say why the requirement is changing (e.g. a market or tooling trend).")
    payload = _payload(form)
    existing = {s["skill"].lower() for s in detail["skills"]}
    if action == "add":
        if skill.lower() in existing:
            raise JobProfileError("The role already requires that skill -- change its requirement instead.")
        if not payload["behavior_indicator"] or len(payload["behavior_indicator"]) < 15:
            raise JobProfileError("Describe the behaviour expected at the target level -- question generation is grounded on it.")
    elif skill.lower() not in existing:
        raise JobProfileError("The role doesn't require that skill.")
    if action != "remove" and not 1 <= payload["target_level"] <= 5:
        raise JobProfileError("Choose a target level from 1 to 5.")
    if action == "remove" and len(existing) <= 1:
        raise JobProfileError("A job profile needs at least one required skill.")
    if trend_id and not any(t["trend_id"] == trend_id for t in store.list_trends()):
        trend_id = None
    change_id = store.add_skill_change(role_id=role_id, action=action, skill=skill, payload=payload, reason=reason,
                                       trend_id=trend_id, proposed_by=user["id"])
    if user["role"] in EDITOR_ROLES:
        _apply(change_id, user["id"], "Applied by the editor who made it.")
        return {"change_id": change_id, "applied": True}
    editors = [u["user_id"] for u in db.list_users() if u["role"] in EDITOR_ROLES]
    engagement.notify(editors, "job_change", f"Skill requirement proposal: {detail['role']['role_name']}",
                      f"{ACTIONS[action]} · {skill}. {reason}", f"/org/job-profiles/{role_id}")
    return {"change_id": change_id, "applied": False}


def _apply(change_id: str, by: str, note: str) -> None:
    change = store.get_skill_change(change_id)
    if not store.apply_skill_change(change["role_id"], change["action"], change["skill"], change["payload"]):
        store.mark_skill_change(change_id, "rejected", by, "Could not be applied: the role changed since it was proposed.")
        raise JobProfileError("That change no longer applies to the current requirements.")
    store.mark_skill_change(change_id, "applied", by, note)


def decide_change(change_id: str, decision: str, note: str, user: dict) -> None:
    if user["role"] not in EDITOR_ROLES:
        raise PermissionError("Only admins, HR partners and leaders approve requirement changes.")
    change = store.get_skill_change(change_id)
    if not change or change["status"] != "proposed":
        raise JobProfileError("This proposal has already been decided.")
    note = sanitise_text(note, 600)
    if decision == "approve":
        _apply(change_id, user["id"], note or "Approved.")
    elif decision == "reject":
        if len(note) < 5:
            raise JobProfileError("Tell the manager why.")
        store.mark_skill_change(change_id, "rejected", user["id"], note)
    else:
        raise JobProfileError("Approve or reject the proposal.")
    engagement.notify([change["proposed_by"]], "job_change",
                      f"Your skill requirement proposal was {'approved' if decision == 'approve' else 'rejected'}",
                      f"{change['skill']}: {note or 'Approved.'}", f"/org/job-profiles/{change['role_id']}")


def add_trend(*, skill: str, tower: str, direction: str, note: str, source: str, user: dict) -> str:
    if user["role"] not in EDITOR_ROLES + PROPOSER_ROLES:
        raise PermissionError("Only managers, leaders, HR partners and admins log trends.")
    skill, note = sanitise_text(skill, 120), sanitise_text(note, 600)
    if len(skill) < 2 or direction not in DIRECTIONS or len(note) < 10:
        raise JobProfileError("Name the skill, its direction and what is changing in the market.")
    return store.add_trend(skill=skill, tower=tower if tower in towers() else None, direction=direction, note=note,
                           source=sanitise_text(source, 200), added_by=user["id"])


# ---------------------------------------------------------------------------
# Skill catalogue (admins and HR partners)
# ---------------------------------------------------------------------------

def add_catalogue_skill(*, skill: str, tower: str, capability: str, indicators: list[str], user: dict) -> str:
    """A new skill in the catalogue. Blank level indicators take the generic L1-L5 wording."""
    if user["role"] not in CATALOGUE_ROLES:
        raise PermissionError("Only admins and HR partners maintain the skill catalogue.")
    skill, capability = sanitise_text(skill, 80), sanitise_text(capability, 60)
    if len(skill) < 2:
        raise JobProfileError("Name the skill.")
    if tower and tower not in towers():
        raise JobProfileError("Unknown vertical.")
    generic = store.generic_indicators()
    levels = [sanitise_text(indicators[n] if n < len(indicators) else "", 300) or generic.get(n + 1) or f"Level {n + 1}"
              for n in range(5)]
    skill_id = store.create_skill(tower=tower or None, skill=skill, capability=capability or "Technical Expertise",
                                  indicators=levels)
    if not skill_id:
        raise JobProfileError("That skill is already in the catalogue.")
    return skill_id


# ---------------------------------------------------------------------------
# Readiness: what a job profile needs before people can be assessed against it
# ---------------------------------------------------------------------------

MIN_SKILLS = 3
MIN_BANK_QUESTIONS = 5


def readiness(role_id: str) -> dict | None:
    detail = role_detail(role_id)
    if not detail:
        return None
    skills = detail["skills"]
    counts = store.role_readiness_counts(role_id)
    trained = sum(1 for s in skills if s["skill"].lower() in counts["trained_skills"])
    concrete = sum(1 for s in skills if len((s.get("behavior_indicator") or "").strip()) >= 15)
    checks = [
        {"key": "department", "label": "Linked to a department", "ok": bool(detail["profile"].get("dept_id")),
         "detail": detail["profile"].get("department_name") or "Leader scope and HR reporting follow the department."},
        {"key": "skills", "label": f"At least {MIN_SKILLS} required skills", "ok": len(skills) >= MIN_SKILLS,
         "detail": f"{len(skills)} required"},
        {"key": "critical", "label": "At least one critical skill", "ok": any(s["is_critical"] for s in skills),
         "detail": f"{sum(1 for s in skills if s['is_critical'])} critical (promotion readiness needs them)"},
        {"key": "indicators", "label": "Every skill has a concrete behaviour indicator", "ok": bool(skills) and concrete == len(skills),
         "detail": f"{concrete} of {len(skills)}: question generation is grounded on them"},
        {"key": "questions", "label": f"{MIN_BANK_QUESTIONS}+ SME-approved bank questions", "ok": counts["approved_questions"] >= MIN_BANK_QUESTIONS,
         "detail": f"{counts['approved_questions']} approved: needed to auto-assign baseline assessments",
         "link": f"/sme/generate?role={role_id}", "link_label": "SME: generate questions"},
        {"key": "training", "label": "Training mapped to the skills", "ok": bool(skills) and trained == len(skills),
         "detail": f"{trained} of {len(skills)} skills have a catalogue course (TNI recommends them)"},
    ]
    done = sum(1 for c in checks if c["ok"])
    return {"checks": checks, "done": done, "total": len(checks), "pct": round(100 * done / len(checks)),
            "people": detail["people"]}
