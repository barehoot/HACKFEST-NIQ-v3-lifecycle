"""Agent 8 in the portal: AI-pitched skills for a job profile and new-skill
recommendations for the catalogue, with a person deciding every one.

  suggest_for_role       role text + catalogue + current requirements + trends -> Agent 8
                         -> a pending batch of skill suggestions on the job profile
  suggest_for_catalogue  a vertical (and an optional focus) -> new catalogue skills
  accept / dismiss       an editor (admin, HR, leader) accepting a role suggestion applies
                         it as a recorded requirement change; a manager's acceptance
                         becomes a proposal. Catalogue suggestions are accepted by admins
                         and HR partners and land in skill_master.

The suggestion is re-checked here whatever the agent returned: names are cleaned, a
catalogue skill keeps its catalogue spelling, levels are clamped to 1-5, and a missing
behaviour indicator is taken from the catalogue's own level description.
"""
from __future__ import annotations

import uuid

from domain.governance import sanitise_text
from services import api_client, db, engagement, job_profiles, lifecycle_store as store
from services.backend_api import BackendAPIError

UNAVAILABLE = "The AI skill advisor is unavailable right now. Add skills by hand, or try again shortly."


class AdviceError(ValueError):
    pass


def _catalogue(tower: str | None = None) -> list[dict]:
    rows = store.skill_master_rows()
    # The role's own vertical first, so a long catalogue never crowds out the relevant skills.
    rows.sort(key=lambda r: (r["tower"] != tower, r["tower"] or "", r["skill"]))
    return [{"skill": r["skill"], "capability": r["capability"]} for r in rows[:400]]


def _trends(tower: str | None) -> list[dict]:
    return [{"skill": t["skill"], "direction": t["direction"], "note": t["note"] or ""} for t in store.list_trends(tower)[:15]]


def _indicator_for(skill: str, level: int) -> str:
    master = store.skill_by_name(skill) or {}
    return (master.get(f"l{level}_indicator") or "").strip()[:400]


def _clean_role_item(item: dict, catalogue: dict[str, dict], current: set[str]) -> dict | None:
    name = sanitise_text(item.get("skill"), 80)
    if len(name) < 2 or name.lower() in current:
        return None
    match = catalogue.get(name.lower())
    try:
        level = max(1, min(5, int(item.get("target_level") or 3)))
    except (TypeError, ValueError):
        level = 3
    skill = match["skill"] if match else name
    indicator = sanitise_text(item.get("behavior_indicator"), 400)
    if len(indicator) < 15:
        indicator = _indicator_for(skill, level) if match else indicator
    return {"skill": skill, "existing": bool(match),
            "capability": sanitise_text(item.get("capability"), 60) or (match or {}).get("capability") or "Technical Expertise",
            "target_level": level, "is_critical": bool(item.get("is_critical")), "behavior_indicator": indicator,
            "rationale": sanitise_text(item.get("rationale"), 300)}


def suggest_for_role(role_id: str, user: dict) -> dict:
    """Asks Agent 8 for the skills this job profile should require. Returns {"count", "generated_by", "summary"}."""
    if user["role"] not in job_profiles.EDITOR_ROLES + job_profiles.PROPOSER_ROLES:
        raise PermissionError("Only managers, leaders, HR partners and admins request skill suggestions.")
    detail = job_profiles.role_detail(role_id)
    if not detail:
        raise AdviceError("Unknown job profile.")
    role, text = detail["role"], store.role_description(role_id)
    current = [s["skill"] for s in detail["skills"]]
    catalogue = _catalogue(role["tower"])
    payload = {"mode": "role", "tower": role["tower"], "role_name": role["role_name"], "grade": role["role_grade"] or "",
               "summary": (text.get("role_summary") or detail["profile"].get("description") or "")[:1000],
               "responsibilities": (text.get("responsibility_extract") or "")[:1500],
               "skills_text": (text.get("skills_extract") or "")[:800], "current": current[:40],
               "catalogue": catalogue, "trends": _trends(role["tower"])}
    try:
        result = api_client.backend_skill_advice(payload) or {}
    except BackendAPIError as exc:
        raise AdviceError(UNAVAILABLE) from exc
    known = {c["skill"].lower(): c for c in catalogue}
    taken = {s.lower() for s in current}
    items = []
    for raw in result.get("skills") or []:
        item = _clean_role_item(raw, known, taken) if isinstance(raw, dict) else None
        if item:
            taken.add(item["skill"].lower())
            items.append(item)
    generated_by = result.get("generated_by") if result.get("generated_by") in ("agent", "rules") else "unavailable"
    store.add_skill_suggestions(batch_id=f"B-{uuid.uuid4().hex[:10].upper()}", scope="role", role_id=role_id,
                                tower=role["tower"], items=items[:10], generated_by=generated_by, created_by=user["id"])
    return {"count": len(items[:10]), "generated_by": generated_by, "summary": sanitise_text(result.get("summary"), 400)}


def suggest_for_catalogue(tower: str, focus: str, user: dict) -> dict:
    if user["role"] not in job_profiles.CATALOGUE_ROLES:
        raise PermissionError("Only admins and HR partners maintain the skill catalogue.")
    tower = tower if tower in job_profiles.towers() else ""
    payload = {"mode": "catalogue", "tower": tower, "focus": sanitise_text(focus, 500),
               "catalogue": _catalogue(tower or None), "trends": _trends(tower or None)}
    try:
        result = api_client.backend_skill_advice(payload) or {}
    except BackendAPIError as exc:
        raise AdviceError(UNAVAILABLE) from exc
    existing = {c["skill"].lower() for c in store.skill_master_rows()}
    items = []
    for raw in result.get("skills") or []:
        if not isinstance(raw, dict):
            continue
        name = sanitise_text(raw.get("skill"), 80)
        if len(name) < 2 or name.lower() in existing:
            continue
        existing.add(name.lower())
        indicators = [sanitise_text(i, 300) for i in (raw.get("indicators") or []) if isinstance(i, str)][:5]
        items.append({"skill": name, "capability": sanitise_text(raw.get("capability"), 60) or "Digital & Automation",
                      "direction": raw.get("direction") if raw.get("direction") in ("emerging", "rising") else "emerging",
                      "rationale": sanitise_text(raw.get("rationale"), 300),
                      "indicators": indicators if len(indicators) == 5 else []})
    generated_by = result.get("generated_by") if result.get("generated_by") in ("agent", "rules") else "unavailable"
    store.add_skill_suggestions(batch_id=f"B-{uuid.uuid4().hex[:10].upper()}", scope="catalogue", role_id=None,
                                tower=tower or None, items=items[:6], generated_by=generated_by, created_by=user["id"])
    return {"count": len(items[:6]), "generated_by": generated_by, "summary": sanitise_text(result.get("summary"), 400)}


def _pending(suggestion_id: str, scope: str) -> dict:
    suggestion = store.get_skill_suggestion(sanitise_text(suggestion_id, 40))
    if not suggestion or suggestion["scope"] != scope or suggestion["status"] != "pending":
        raise AdviceError("This suggestion has already been decided.")
    return suggestion


def accept_role_suggestion(suggestion_id: str, overrides: dict, user: dict) -> dict:
    """Applies (editors) or proposes (managers) the suggested skill as a requirement change,
    with the person's edits to level, criticality and behaviour. Returns request_change's result."""
    suggestion = _pending(suggestion_id, "role")
    item = {**suggestion["payload"], **{k: v for k, v in overrides.items() if v not in (None, "")}}
    origin = "Agent 8 (AI) recommendation" if suggestion["generated_by"] == "agent" else "Agent 8 rules-based suggestion"
    reason = f"{origin}, reviewed by a person: {item.get('rationale') or 'pitched for this job profile.'}"
    result = job_profiles.request_change(
        suggestion["role_id"], action="add", skill=suggestion["skill"],
        form={"target_level": item.get("target_level"), "is_critical": item.get("is_critical"),
              "capability": item.get("capability"), "behavior_indicator": item.get("behavior_indicator")},
        reason=reason[:600], trend_id=None, user=user)
    store.mark_skill_suggestion(suggestion["suggestion_id"], "accepted" if result["applied"] else "proposed", user["id"])
    return result


def accept_catalogue_suggestion(suggestion_id: str, overrides: dict, user: dict) -> str:
    suggestion = _pending(suggestion_id, "catalogue")
    item = suggestion["payload"]
    skill_id = job_profiles.add_catalogue_skill(
        skill=overrides.get("skill") or suggestion["skill"], tower=suggestion["tower"] or "",
        capability=overrides.get("capability") or item.get("capability") or "", indicators=item.get("indicators") or [],
        user=user)
    store.mark_skill_suggestion(suggestion["suggestion_id"], "accepted", user["id"])
    return skill_id


def dismiss(suggestion_id: str, user: dict) -> dict:
    suggestion = store.get_skill_suggestion(sanitise_text(suggestion_id, 40))
    if not suggestion or suggestion["status"] != "pending":
        raise AdviceError("This suggestion has already been decided.")
    allowed = job_profiles.CATALOGUE_ROLES if suggestion["scope"] == "catalogue" else job_profiles.EDITOR_ROLES + job_profiles.PROPOSER_ROLES
    if user["role"] not in allowed:
        raise PermissionError("Access restricted")
    store.mark_skill_suggestion(suggestion["suggestion_id"], "dismissed", user["id"])
    return suggestion


def notify_smes_new_role(role_id: str, role_name: str) -> int:
    """A freshly onboarded job profile has no approved questions yet: tell the SMEs."""
    smes = [u["user_id"] for u in db.list_users() if u["role"] == "sme"]
    return engagement.notify(smes, "job_change", f"New job profile needs questions: {role_name}",
                             "Its required skills are set. Generate and approve bank questions so people in the role "
                             "can be assessed.", f"/sme/generate?role={role_id}")
