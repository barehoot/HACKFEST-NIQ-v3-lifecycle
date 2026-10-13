"""Organisation set-up: verticals, departments, job profiles and the skills they
require, the skill catalogue, market trends, and employees' HR attributes
(department, hire date). Onboarding a new job profile is a guided flow: vertical ->
department -> job profile -> Agent 8 pitches the required skills -> readiness.

Who can do what (enforced here and again in services/job_profiles.py and skill_advisor.py):
  admin, hr, leader  create verticals, departments and job profiles; apply requirement
                     changes and accept Agent 8's skill pitches; approve managers' proposals
  manager            propose requirement changes (an accepted AI pitch becomes a proposal); log trends
  admin, hr          maintain the skill catalogue (add skills, accept Agent 8 recommendations);
                     set an employee's department and hire date (Workday-synced in production)
"""
from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from domain import governance
from services import auth, db, job_profiles, lifecycle_store as store, performance, probation, security, skill_advisor
from routes.web import flash, pop_flash, render

router = APIRouter()
CONFIGURERS = ("admin", "leader", "manager", "hr")
EDITORS = job_profiles.EDITOR_ROLES
HR_ADMIN = ("admin", "hr")
NEW = "__new__"


def _leaders() -> list[dict]:
    return [u for u in db.list_users() if u["role"] == "leader"]


def catalogue_page(request: Request, status_code: int = 200, error: str | None = None):
    return render(request, "org/job_profiles.html", status_code=status_code, data=job_profiles.catalogue(),
                  towers=job_profiles.towers(), directions=job_profiles.DIRECTIONS,
                  leaders=_leaders(), error=error, notice=pop_flash(request))


def detail_page(request: Request, role_id: str, status_code: int = 200, error: str | None = None):
    role_id = security.clean_form_text(role_id, 120)
    detail = job_profiles.role_detail(role_id)
    if not detail:
        raise HTTPException(404, "Job profile not found")
    return render(request, "org/job_profile_detail.html", status_code=status_code, d=detail, actions=job_profiles.ACTIONS,
                  directions=job_profiles.DIRECTIONS, readiness=job_profiles.readiness(role_id),
                  suggestions=store.list_skill_suggestions(scope="role", role_id=role_id),
                  error=error or request.session.pop("flash_error", None), notice=pop_flash(request))


@router.get("/org/job-profiles")
def org_job_profiles(request: Request, user: dict = Depends(auth.require_role(*CONFIGURERS))):
    return catalogue_page(request)


@router.get("/org/job-profiles/{role_id}")
def org_job_profile(request: Request, role_id: str, user: dict = Depends(auth.require_role(*CONFIGURERS))):
    return detail_page(request, role_id)


@router.post("/org/departments")
def org_department_create(request: Request, name: Annotated[str, Form()] = "", tower: Annotated[str, Form()] = "",
                          description: Annotated[str, Form()] = "", leader_id: Annotated[str, Form()] = "",
                          csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role(*CONFIGURERS))):
    security.validate_csrf(request, csrf_token)
    try:
        dept_id = job_profiles.create_department(name=name, tower=tower, description=description,
                                                 leader_id=security.clean_form_text(leader_id, 80) or None, user=user)
    except PermissionError:
        raise HTTPException(403, "Access restricted")
    except job_profiles.JobProfileError as exc:
        return catalogue_page(request, status_code=400, error=str(exc))
    governance.record_audit(user["id"], user["role"], "department_created", "/org/departments", details={"dept_id": dept_id})
    flash(request, f"Department “{security.clean_form_text(name, 80)}” created.")
    return RedirectResponse("/org/job-profiles#departments", status_code=303)


@router.post("/org/job-profiles")
def org_job_profile_create(request: Request, name: Annotated[str, Form()] = "", tower: Annotated[str, Form()] = "",
                           process_group: Annotated[str, Form()] = "", grade: Annotated[str, Form()] = "",
                           dept_id: Annotated[str, Form()] = "", summary: Annotated[str, Form()] = "",
                           responsibilities: Annotated[str, Form()] = "", skills_text: Annotated[str, Form()] = "",
                           csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role(*CONFIGURERS))):
    security.validate_csrf(request, csrf_token)
    try:
        role_id = job_profiles.create_role(name=name, tower=tower, process_group=process_group, grade=grade,
                                           dept_id=security.clean_form_text(dept_id, 40) or None, summary=summary,
                                           responsibilities=responsibilities, skills_text=skills_text, user=user)
    except PermissionError:
        raise HTTPException(403, "Access restricted")
    except job_profiles.JobProfileError as exc:
        return catalogue_page(request, status_code=400, error=str(exc))
    governance.record_audit(user["id"], user["role"], "job_profile_created", "/org/job-profiles", details={"role_id": role_id})
    flash(request, "Job profile created. Add its required skills below; resume matching picks it up after the nightly re-index.")
    return RedirectResponse(f"/org/job-profiles/{role_id}", status_code=303)


@router.post("/org/job-profiles/{role_id}/skills")
async def org_skill_change(request: Request, role_id: str, user: dict = Depends(auth.require_role(*CONFIGURERS))):
    form = await request.form()
    security.validate_csrf(request, form.get("csrf_token", ""))
    role_id = security.clean_form_text(role_id, 120)
    fields = {k: security.clean_form_text(form.get(k), 400) for k in
              ("target_level", "is_critical", "capability", "behavior_indicator")}
    try:
        result = job_profiles.request_change(
            role_id, action=security.clean_form_text(form.get("action"), 10), skill=form.get("skill") or "",
            form=fields, reason=form.get("reason") or "", trend_id=security.clean_form_text(form.get("trend_id"), 40) or None,
            user=user)
    except PermissionError:
        raise HTTPException(403, "Access restricted")
    except job_profiles.JobProfileError as exc:
        return detail_page(request, role_id, status_code=400, error=str(exc))
    governance.record_audit(user["id"], user["role"], "skill_requirement_applied" if result["applied"] else "skill_requirement_proposed",
                            f"/org/job-profiles/{role_id}", details={"change_id": result["change_id"]})
    if result["applied"]:
        # Everyone in the role is re-scored against the new requirement now, not at the next worker run.
        affected = sorted(store.status_ids({"role_id": role_id}))
        performance.refresh(affected, escalate=False)
        flash(request, f"Requirement updated. {len(affected)} employee(s) in this role were re-scored.")
    else:
        flash(request, "Proposal sent to the admins and leaders for approval.")
    return RedirectResponse(f"/org/job-profiles/{role_id}", status_code=303)


@router.post("/org/changes/{change_id}/decision")
def org_change_decision(request: Request, change_id: str, decision: Annotated[str, Form()] = "",
                        note: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "",
                        user: dict = Depends(auth.require_role(*CONFIGURERS))):
    security.validate_csrf(request, csrf_token)
    change = store.get_skill_change(security.clean_form_text(change_id, 40))
    if not change:
        raise HTTPException(404, "Proposal not found")
    try:
        job_profiles.decide_change(change["change_id"], decision, note, user)
    except PermissionError:
        raise HTTPException(403, "Access restricted")
    except job_profiles.JobProfileError as exc:
        return detail_page(request, change["role_id"], status_code=400, error=str(exc))
    governance.record_audit(user["id"], user["role"], f"skill_requirement_{decision}", f"/org/changes/{change_id}")
    flash(request, "Proposal approved and applied." if decision == "approve" else "Proposal rejected; the manager was told why.")
    return RedirectResponse(f"/org/job-profiles/{change['role_id']}", status_code=303)


@router.post("/org/trends")
def org_trend_add(request: Request, skill: Annotated[str, Form()] = "", tower: Annotated[str, Form()] = "",
                  direction: Annotated[str, Form()] = "", note: Annotated[str, Form()] = "",
                  source: Annotated[str, Form()] = "", back: Annotated[str, Form()] = "",
                  csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role(*CONFIGURERS))):
    security.validate_csrf(request, csrf_token)
    try:
        job_profiles.add_trend(skill=skill, tower=tower, direction=direction, note=note, source=source, user=user)
    except PermissionError:
        raise HTTPException(403, "Access restricted")
    except job_profiles.JobProfileError as exc:
        return catalogue_page(request, status_code=400, error=str(exc))
    governance.record_audit(user["id"], user["role"], "trend_logged", "/org/trends")
    flash(request, "Trend logged.")
    # Only an in-app path is honoured as the return target (no open redirect).
    target = back if back.startswith("/org/job-profiles") and "//" not in back else "/org/job-profiles#trends"
    return RedirectResponse(target, status_code=303)


@router.post("/org/verticals")
def org_vertical_create(request: Request, code: Annotated[str, Form()] = "", name: Annotated[str, Form()] = "",
                        description: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "",
                        user: dict = Depends(auth.require_role(*CONFIGURERS))):
    security.validate_csrf(request, csrf_token)
    try:
        created = job_profiles.create_vertical(code=code, name=name, description=description, user=user)
    except PermissionError:
        raise HTTPException(403, "Access restricted")
    except job_profiles.JobProfileError as exc:
        return catalogue_page(request, status_code=400, error=str(exc))
    governance.record_audit(user["id"], user["role"], "vertical_created", "/org/verticals", details={"code": created})
    flash(request, f"Vertical {created} created. Add its departments and job profiles, or onboard a role into it.")
    return RedirectResponse("/org/job-profiles#verticals", status_code=303)


# ---------------------------------------------------------------------------
# Onboarding a new job profile: vertical -> department -> job -> AI-pitched skills -> readiness
# ---------------------------------------------------------------------------

def onboard_page(request: Request, status_code: int = 200, error: str | None = None, form: dict | None = None):
    return render(request, "org/onboard.html", status_code=status_code, verticals=store.list_verticals(),
                  departments=store.list_departments(), leaders=_leaders(), form=form or {}, error=error,
                  notice=pop_flash(request))


@router.get("/org/onboard")
def org_onboard(request: Request, posting: str = "", user: dict = Depends(auth.require_role(*EDITORS))):
    # From an unmapped internal posting: the JD prefills the job profile, and the new profile is mapped back to it.
    item = store.get_posting(security.clean_form_text(posting, 80)) if posting else None
    form = {"name": item["title"], "summary": (item["description"] or "")[:1000], "responsibilities": (item["description"] or "")[:1500],
            "skills_text": (item["qualifications"] or "").replace("\n", ", ")[:800], "posting_id": item["posting_id"],
            "ai": True} if item else None
    return onboard_page(request, form=form)


@router.post("/org/onboard")
async def org_onboard_submit(request: Request, user: dict = Depends(auth.require_role(*EDITORS))):
    form = await request.form()
    security.validate_csrf(request, form.get("csrf_token", ""))
    f = {k: security.clean_form_text(form.get(k), 1500) for k in (
        "vertical", "new_vertical_code", "new_vertical_name", "new_vertical_description", "department", "new_department_name",
        "new_department_leader", "name", "process_group", "grade", "summary", "responsibilities", "skills_text", "posting_id")}
    f["ai"] = bool(form.get("ai"))
    try:
        # Validate everything first, so a mistake never leaves a half-made vertical or department behind.
        job_profiles.validate_role_fields(name=f["name"], grade=f["grade"], summary=f["summary"])
        if f["vertical"] != NEW and f["vertical"] not in job_profiles.towers():
            raise job_profiles.JobProfileError("Choose a vertical, or create a new one.")
        if f["department"] not in ("", NEW) and not store.get_department(f["department"]):
            raise job_profiles.JobProfileError("Unknown department.")
        if f["department"] == NEW:
            if len(f["new_department_name"]) < 2:
                raise job_profiles.JobProfileError("Name the new department.")
            if f["new_department_name"].lower() in {d["name"].lower() for d in store.list_departments()}:
                raise job_profiles.JobProfileError("A department with that name already exists: choose it instead.")
            if f["new_department_leader"] and f["new_department_leader"] not in {u["user_id"] for u in _leaders()}:
                raise job_profiles.JobProfileError("Choose a leader for the department.")
        if f["vertical"] == NEW:
            code, name = f["new_vertical_code"].upper().replace(" ", ""), f["new_vertical_name"].lower()
            if any(v["code"] == code or v["name"].lower() == name for v in store.list_verticals()):
                raise job_profiles.JobProfileError("A vertical with that code or name already exists: choose it instead.")
        elif job_profiles.role_exists(f["vertical"], f["name"]):
            raise job_profiles.JobProfileError("A job profile with that name already exists in this vertical.")
        tower = f["vertical"]
        if tower == NEW:
            tower = job_profiles.create_vertical(code=f["new_vertical_code"], name=f["new_vertical_name"],
                                                 description=f["new_vertical_description"], user=user)
            governance.record_audit(user["id"], user["role"], "vertical_created", "/org/onboard", details={"code": tower})
        dept_id = f["department"] or None
        if dept_id == NEW:
            dept_id = job_profiles.create_department(name=f["new_department_name"], tower=tower, description="",
                                                     leader_id=f["new_department_leader"] or None, user=user)
            governance.record_audit(user["id"], user["role"], "department_created", "/org/onboard", details={"dept_id": dept_id})
        role_id = job_profiles.create_role(name=f["name"], tower=tower, process_group=f["process_group"], grade=f["grade"],
                                           dept_id=dept_id, summary=f["summary"], responsibilities=f["responsibilities"],
                                           skills_text=f["skills_text"], user=user)
    except PermissionError:
        raise HTTPException(403, "Access restricted")
    except job_profiles.JobProfileError as exc:
        return onboard_page(request, status_code=400, error=str(exc), form=f)
    governance.record_audit(user["id"], user["role"], "job_profile_onboarded", "/org/onboard", details={"role_id": role_id})
    message = "Job profile created."
    if f["posting_id"] and store.get_posting(f["posting_id"]):
        store.map_posting(f["posting_id"], role_id, user["id"])
        message += " The internal posting now uses it, so employees see their readiness for the job."
    if f["ai"]:
        try:
            result = skill_advisor.suggest_for_role(role_id, user)
            governance.record_audit(user["id"], user["role"], "skill_advice_requested", f"/org/onboard/{role_id}",
                                    details={"count": result["count"], "generated_by": result["generated_by"]})
            message += (f" Agent 8 pitched {result['count']} skill(s)" + (" from keyword rules (the AI was unavailable)"
                        if result["generated_by"] == "rules" else "") + ": review them below.")
        except skill_advisor.AdviceError as exc:
            message += f" {exc}"
    flash(request, message)
    return RedirectResponse(f"/org/onboard/{role_id}", status_code=303)


@router.get("/org/onboard/{role_id}")
def org_onboard_role(request: Request, role_id: str, user: dict = Depends(auth.require_role(*EDITORS))):
    role_id = security.clean_form_text(role_id, 120)
    detail = job_profiles.role_detail(role_id)
    if not detail:
        raise HTTPException(404, "Job profile not found")
    return render(request, "org/onboard_role.html", d=detail, readiness=job_profiles.readiness(role_id),
                  suggestions=store.list_skill_suggestions(scope="role", role_id=role_id),
                  notice=pop_flash(request), error=request.session.pop("flash_error", None))


def _role_back(role_id: str, back: str) -> str:
    return f"/org/onboard/{role_id}" if back == "onboard" else f"/org/job-profiles/{role_id}#ai-skills"


@router.post("/org/job-profiles/{role_id}/suggest")
def org_skill_suggest(request: Request, role_id: str, back: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "",
                      user: dict = Depends(auth.require_role(*CONFIGURERS))):
    security.validate_csrf(request, csrf_token)
    role_id = security.clean_form_text(role_id, 120)
    try:
        result = skill_advisor.suggest_for_role(role_id, user)
    except PermissionError:
        raise HTTPException(403, "Access restricted")
    except skill_advisor.AdviceError as exc:
        if not job_profiles.role_detail(role_id):
            raise HTTPException(404, "Job profile not found")
        request.session["flash_error"] = str(exc)
        return RedirectResponse(_role_back(role_id, back), status_code=303)
    governance.record_audit(user["id"], user["role"], "skill_advice_requested", f"/org/job-profiles/{role_id}",
                            details={"count": result["count"], "generated_by": result["generated_by"]})
    flash(request, f"Agent 8 pitched {result['count']} skill(s)" + (
        " from keyword rules (the AI was unavailable)" if result["generated_by"] == "rules" else "") + ". Review them below.")
    return RedirectResponse(_role_back(role_id, back), status_code=303)


@router.post("/org/job-profiles/{role_id}/suggestions")
async def org_skill_suggestions_apply(request: Request, role_id: str, user: dict = Depends(auth.require_role(*CONFIGURERS))):
    """One review decides the whole batch: ticked suggestions are applied (or proposed, for a
    manager) with the person's edits; unticked ones are dismissed."""
    form = await request.form()
    security.validate_csrf(request, form.get("csrf_token", ""))
    role_id = security.clean_form_text(role_id, 120)
    back = security.clean_form_text(form.get("back"), 20)
    detail = job_profiles.role_detail(role_id)
    if not detail:
        raise HTTPException(404, "Job profile not found")
    had_skills = bool(detail["skills"])
    applied, proposed, dismissed, errors = 0, 0, 0, []
    for suggestion in store.list_skill_suggestions(scope="role", role_id=role_id):
        sid = suggestion["suggestion_id"]
        try:
            if form.get(f"accept_{sid}"):
                level = security.clean_form_text(form.get(f"level_{sid}"), 2)
                result = skill_advisor.accept_role_suggestion(sid, {
                    "target_level": int(level) if level.isdigit() else None,
                    "is_critical": bool(form.get(f"critical_{sid}")),
                    "behavior_indicator": security.clean_form_text(form.get(f"behavior_{sid}"), 400),
                }, user)
                applied += result["applied"]
                proposed += not result["applied"]
            else:
                skill_advisor.dismiss(sid, user)
                dismissed += 1
        except PermissionError:
            raise HTTPException(403, "Access restricted")
        except (job_profiles.JobProfileError, skill_advisor.AdviceError) as exc:
            errors.append(f"{suggestion['skill']}: {exc}")
    governance.record_audit(user["id"], user["role"], "skill_advice_reviewed", f"/org/job-profiles/{role_id}",
                            details={"applied": applied, "proposed": proposed, "dismissed": dismissed, "errors": len(errors)})
    if applied:
        affected = sorted(store.status_ids({"role_id": role_id}))
        performance.refresh(affected, escalate=False)
        if back == "onboard" and not had_skills and store.role_readiness_counts(role_id)["approved_questions"] == 0:
            skill_advisor.notify_smes_new_role(role_id, detail["role"]["role_name"])
    parts = [f"{applied} skill(s) applied"] if applied else []
    if proposed:
        parts.append(f"{proposed} sent to an admin, HR partner or leader for approval")
    if dismissed:
        parts.append(f"{dismissed} dismissed")
    flash(request, (", ".join(parts) or "Nothing selected") + "." + (
        " SMEs were asked to generate questions for the role." if back == "onboard" and applied and not had_skills else ""))
    if errors:
        request.session["flash_error"] = "Not applied (fix and accept again): " + "; ".join(errors[:5])
    return RedirectResponse(_role_back(role_id, back), status_code=303)


# ---------------------------------------------------------------------------
# Skill catalogue: add skills by hand, or from Agent 8's recommendations
# ---------------------------------------------------------------------------

def skills_page(request: Request, tower: str = "", status_code: int = 200, error: str | None = None):
    towers = job_profiles.towers()
    tower = tower if tower in towers else ""
    catalogue = store.skill_catalogue(tower or None)
    groups: dict[str, list] = {}
    for skill in catalogue:
        groups.setdefault(skill["tower"] or "Cross-vertical", []).append(skill)
    return render(request, "org/skills.html", status_code=status_code, groups=groups, total=len(catalogue),
                  verticals=store.list_verticals(), selected_tower=tower, generic=store.generic_indicators(),
                  suggestions=store.list_skill_suggestions(scope="catalogue"),
                  recent=store.list_skill_suggestions(scope="catalogue", statuses=("accepted",), limit=8),
                  error=error or request.session.pop("flash_error", None), notice=pop_flash(request))


@router.get("/org/skills")
def org_skills(request: Request, tower: str = "", user: dict = Depends(auth.require_role(*HR_ADMIN))):
    return skills_page(request, security.clean_form_text(tower, 8))


@router.post("/org/skills")
async def org_skill_add(request: Request, user: dict = Depends(auth.require_role(*HR_ADMIN))):
    form = await request.form()
    security.validate_csrf(request, form.get("csrf_token", ""))
    tower = security.clean_form_text(form.get("tower"), 8)
    try:
        skill_id = job_profiles.add_catalogue_skill(
            skill=form.get("skill") or "", tower=tower, capability=form.get("capability") or "",
            indicators=[form.get(f"l{n}") or "" for n in range(1, 6)], user=user)
    except PermissionError:
        raise HTTPException(403, "Access restricted")
    except job_profiles.JobProfileError as exc:
        return skills_page(request, tower, status_code=400, error=str(exc))
    governance.record_audit(user["id"], user["role"], "catalogue_skill_added", "/org/skills", details={"skill_id": skill_id})
    flash(request, f"“{security.clean_form_text(form.get('skill'), 80)}” added to the skill catalogue. "
                   "Job profiles can now require it.")
    return RedirectResponse("/org/skills" + (f"?tower={tower}" if tower else ""), status_code=303)


@router.post("/org/skills/suggest")
def org_skill_recommend(request: Request, tower: Annotated[str, Form()] = "", focus: Annotated[str, Form()] = "",
                        csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role(*HR_ADMIN))):
    security.validate_csrf(request, csrf_token)
    try:
        result = skill_advisor.suggest_for_catalogue(security.clean_form_text(tower, 8), focus, user)
    except PermissionError:
        raise HTTPException(403, "Access restricted")
    except skill_advisor.AdviceError as exc:
        request.session["flash_error"] = str(exc)
        return RedirectResponse("/org/skills#recommend", status_code=303)
    governance.record_audit(user["id"], user["role"], "skill_advice_requested", "/org/skills/suggest",
                            details={"count": result["count"], "generated_by": result["generated_by"]})
    flash(request, f"Agent 8 recommended {result['count']} new skill(s)" + (
        " from logged trends and the watchlist (the AI was unavailable)" if result["generated_by"] == "rules" else "")
        + ". Accept the ones worth adding.")
    return RedirectResponse("/org/skills#recommend", status_code=303)


@router.post("/org/skills/suggestions/{suggestion_id}/{action}")
def org_skill_recommendation_decide(request: Request, suggestion_id: str, action: str, skill: Annotated[str, Form()] = "",
                                    capability: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "",
                                    user: dict = Depends(auth.require_role(*HR_ADMIN))):
    security.validate_csrf(request, csrf_token)
    if action not in ("accept", "dismiss"):
        raise HTTPException(404, "Not found")
    try:
        if action == "accept":
            skill_advisor.accept_catalogue_suggestion(suggestion_id, {"skill": security.clean_form_text(skill, 80),
                                                                       "capability": security.clean_form_text(capability, 60)}, user)
        else:
            skill_advisor.dismiss(suggestion_id, user)
    except PermissionError:
        raise HTTPException(403, "Access restricted")
    except (job_profiles.JobProfileError, skill_advisor.AdviceError) as exc:
        return skills_page(request, status_code=400, error=str(exc))
    governance.record_audit(user["id"], user["role"], f"catalogue_suggestion_{action}", f"/org/skills/suggestions/{suggestion_id}")
    flash(request, "Added to the skill catalogue." if action == "accept" else "Recommendation dismissed.")
    return RedirectResponse("/org/skills#recommend", status_code=303)


# ---------------------------------------------------------------------------
# HR attributes: department and hire date
# ---------------------------------------------------------------------------

@router.get("/org/people")
def org_people(request: Request, q: str = "", user: dict = Depends(auth.require_role(*HR_ADMIN))):
    q = security.clean_form_text(q, 60).lower()
    employees = [e for e in db.list_employees() if not q or q in (e["full_name"] or "").lower() or q in e["user_id"].lower()]
    profiles = store.profiles_for([e["user_id"] for e in employees[:200]])
    return render(request, "org/people.html", employees=employees[:200], total=len(employees), profiles=profiles,
                  departments=store.list_departments(), q=q, notice=pop_flash(request))


@router.post("/org/people/{user_id}")
def org_person_update(request: Request, user_id: str, dept_id: Annotated[str, Form()] = "",
                      hire_date: Annotated[str, Form()] = "", location: Annotated[str, Form()] = "",
                      csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role(*HR_ADMIN))):
    security.validate_csrf(request, csrf_token)
    target = db.get_user(security.clean_form_text(user_id, 80))
    if not target or target["role"] != "employee":
        raise HTTPException(404, "Employee not found")
    dept_id = security.clean_form_text(dept_id, 40) or None
    if dept_id and not store.get_department(dept_id):
        raise HTTPException(400, "Unknown department")
    try:
        hired = date.fromisoformat(hire_date).isoformat() if hire_date else None
    except ValueError:
        raise HTTPException(400, "Enter the hire date as YYYY-MM-DD")
    store.upsert_profile(target["user_id"], hire_date=hired, dept_id=dept_id, location=security.clean_form_text(location, 80) or None)
    created = probation.ensure_for_new_joiners(date.today())
    performance.refresh([target["user_id"]], escalate=False)
    governance.record_audit(user["id"], user["role"], "employee_profile_updated", f"/org/people/{target['user_id']}")
    flash(request, f"Updated {target['full_name']}." + (" Probation tracking started." if created else ""))
    return RedirectResponse("/org/people", status_code=303)
