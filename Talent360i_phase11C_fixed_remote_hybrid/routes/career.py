"""Career mobility routes: the employee's Career & Mobility page (aspirations across
departments and verticals, gap analysis, CV skill evidence, upskilling onto the calendar,
internal postings and applications), the hiring managers' talent pool, and the internal
jobs feed HR maps to job profiles.

Access: an employee acts only on their own aspirations, CV claims and applications.
Managers, leaders, HR partners and admins search the talent pool; what they see of a
candidate follows mobility.talent_pool (performance data only for people who opted in or
are already in the viewer's scope), and every talent-card view is audited. Admins and HR
partners sync and map postings.
"""
from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse

from domain import governance
from services import api_client, auth, db, job_profiles, lifecycle_store as store, mobility, progression, schedule, security
from services import smartrecruiters, tni
from services.backend_api import BackendAPIError
from routes.web import employee_scope, flash, pop_flash, render

router = APIRouter()
EMPLOYEE = ("employee", "admin")
POOL = mobility.POOL_VIEWERS
HR_ADMIN = ("admin", "hr")


# ---------------------------------------------------------------------------
# The employee's Career & Mobility page
# ---------------------------------------------------------------------------

def career_page(request: Request, user: dict, status_code: int = 200, error: str | None = None, q: str = "", tower: str = ""):
    uid = employee_scope(user)
    postings = smartrecruiters.search(q, tower)
    return render(request, "employee/career.html", status_code=status_code, aspirations=mobility.aspirations(uid),
                  fit=mobility.fit_for_roles(uid, [p["role_id"] for p in postings]),
                  roles=mobility.role_options(), current=tni.current_role(uid),
                  status=store.status_rows([uid]).get(uid), claims=store.cv_claims([uid]).get(uid, []),
                  cv_targets=mobility.cv_targets(uid), postings=postings, q=q, tower=tower,
                  towers=job_profiles.towers(), connected=smartrecruiters.configured(),
                  applications=store.list_applications(user_id=uid), max_aspirations=mobility.MAX_ASPIRATIONS,
                  today=date.today().isoformat(), error=error or request.session.pop("flash_error", None),
                  notice=pop_flash(request))


@router.get("/employee/career")
def employee_career(request: Request, q: str = "", tower: str = "", user: dict = Depends(auth.require_role(*EMPLOYEE))):
    return career_page(request, user, q=security.clean_form_text(q, 80), tower=security.clean_form_text(tower, 8))


def _back(message: str | None = None, error: str | None = None, request: Request | None = None, anchor: str = ""):
    if request is not None:
        if message:
            flash(request, message)
        if error:
            request.session["flash_error"] = error
    return RedirectResponse("/employee/career" + (f"#{anchor}" if anchor else ""), status_code=303)


@router.post("/employee/career/aspirations")
def career_aspiration_add(request: Request, role_id: Annotated[str, Form()] = "", posting_id: Annotated[str, Form()] = "",
                          note: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "",
                          user: dict = Depends(auth.require_role(*EMPLOYEE))):
    security.validate_csrf(request, csrf_token)
    try:
        mobility.add_aspiration(employee_scope(user), role_id=role_id, posting_id=posting_id, note=note)
    except mobility.MobilityError as exc:
        return _back(error=str(exc), request=request, anchor="aspirations")
    governance.record_audit(user["id"], user["role"], "aspiration_added", "/employee/career/aspirations",
                            details={"role_id": security.clean_form_text(role_id, 120) or None})
    return _back("Aspiration added: your gap analysis and skill-up path are below.", request=request, anchor="aspirations")


@router.post("/employee/career/aspirations/{aspiration_id}/withdraw")
def career_aspiration_withdraw(request: Request, aspiration_id: str, csrf_token: Annotated[str, Form()] = "",
                               user: dict = Depends(auth.require_role(*EMPLOYEE))):
    security.validate_csrf(request, csrf_token)
    if not store.withdraw_aspiration(security.clean_form_text(aspiration_id, 40), employee_scope(user)):
        raise HTTPException(404, "Aspiration not found")
    return _back("Aspiration withdrawn.", request=request, anchor="aspirations")


@router.post("/employee/career/cv")
async def career_cv_upload(request: Request, resume: Annotated[UploadFile | None, File()] = None,
                           csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role(*EMPLOYEE))):
    security.validate_csrf(request, csrf_token)
    uid = employee_scope(user)
    if resume is None or not resume.filename:
        return _back(error="Choose your CV (PDF, DOCX or TXT).", request=request, anchor="cv")
    try:
        data = await resume.read(security.MAX_UPLOAD_BYTES + 1)
        safe_name = security.validate_upload(resume.filename, data)
    except ValueError as exc:
        return _back(error=str(exc), request=request, anchor="cv")
    targets = mobility.cv_targets(uid)
    if not targets:
        return _back(error="Add a career aspiration first, so there are skills to look for.", request=request, anchor="cv")
    try:
        result = api_client.backend_resume_skills(data, safe_name, targets) or {}
    except BackendAPIError:
        return _back(error="Your CV could not be read right now. Please try again shortly.", request=request, anchor="cv")
    count = mobility.save_cv_claims(uid, result, safe_name)
    governance.record_audit(user["id"], user["role"], "cv_skills_assessed", "/employee/career/cv",
                            details={"claims": count, "generated_by": result.get("generated_by")})
    return _back(f"Your CV gives evidence for {count} skill(s). The CV itself is not stored; only the skill, the level it "
                 "claims and a short quote are kept.", request=request, anchor="cv")


@router.post("/employee/career/cv/clear")
def career_cv_clear(request: Request, csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role(*EMPLOYEE))):
    security.validate_csrf(request, csrf_token)
    store.clear_cv_claims(employee_scope(user))
    return _back("Your CV skill claims were removed.", request=request, anchor="cv")


@router.post("/employee/career/upskill")
def career_upskill(request: Request, role_id: Annotated[str, Form()] = "", start_date: Annotated[str, Form()] = "",
                   cadence_days: Annotated[int, Form()] = 7, csrf_token: Annotated[str, Form()] = "",
                   user: dict = Depends(auth.require_role(*EMPLOYEE))):
    security.validate_csrf(request, csrf_token)
    uid = employee_scope(user)
    role_id = security.clean_form_text(role_id, 120)
    if not any(a["role_id"] == role_id for a in store.list_aspirations(user_id=uid)):
        raise HTTPException(404, "Aspiration not found")
    try:
        start = schedule.parse_date(start_date)
        if start < date.today():
            raise ValueError("Choose today or a future start date.")
        result = mobility.book_upskilling(uid, role_id, start=start, cadence_days=14 if cadence_days == 14 else 7, by=user["id"])
    except (ValueError, mobility.MobilityError) as exc:
        return _back(error=str(exc), request=request, anchor="aspirations")
    governance.record_audit(user["id"], user["role"], "upskilling_booked", "/employee/career/upskill",
                            details={"role_id": role_id, "created": result["created"]})
    message = (f"Booked {result['created']} upskilling session(s) toward {result['role']['role_name']} on your calendar."
               if result["created"] else "Nothing new to book: every gap course is already on your calendar.")
    if result["uncovered"]:
        message += f" No catalogue course yet for: {', '.join(result['uncovered'][:4])} (ask your manager for a stretch task)."
    return _back(message, request=request, anchor="aspirations")


@router.post("/employee/career/apply")
def career_apply(request: Request, role_id: Annotated[str, Form()] = "", posting_id: Annotated[str, Form()] = "",
                 note: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "",
                 user: dict = Depends(auth.require_role(*EMPLOYEE))):
    security.validate_csrf(request, csrf_token)
    try:
        mobility.apply(employee_scope(user), role_id=role_id, posting_id=posting_id, note=note)
    except mobility.MobilityError as exc:
        return _back(error=str(exc), request=request, anchor="applications")
    governance.record_audit(user["id"], user["role"], "internal_application", "/employee/career/apply",
                            details={"role_id": security.clean_form_text(role_id, 120) or None,
                                     "posting_id": security.clean_form_text(posting_id, 80) or None})
    return _back("Application sent. The hiring contact and your manager have been told.", request=request, anchor="applications")


@router.post("/employee/career/applications/{application_id}/withdraw")
def career_application_withdraw(request: Request, application_id: str, csrf_token: Annotated[str, Form()] = "",
                                user: dict = Depends(auth.require_role(*EMPLOYEE))):
    security.validate_csrf(request, csrf_token)
    if not mobility.withdraw_application(employee_scope(user), application_id):
        raise HTTPException(404, "Application not found")
    return _back("Application withdrawn.", request=request, anchor="applications")


@router.get("/jobs/{posting_id}")
def job_posting(request: Request, posting_id: str, user: dict = Depends(auth.require_role(*EMPLOYEE, *POOL))):
    posting = store.get_posting(security.clean_form_text(posting_id, 80))
    if not posting:
        raise HTTPException(404, "Posting not found")
    analysis = None
    if posting["role_id"] and user["role"] in EMPLOYEE:
        analysis = mobility.gap_analysis(employee_scope(user), posting["role_id"])
    return render(request, "career/posting.html", posting=posting, analysis=analysis,
                  skills=db.role_skill_details(posting["role_id"]) if posting["role_id"] else [],
                  aspiring=any(a["posting_id"] == posting["posting_id"] or (posting["role_id"] and a["role_id"] == posting["role_id"])
                               for a in store.list_aspirations(user_id=employee_scope(user))) if user["role"] in EMPLOYEE else False)


# ---------------------------------------------------------------------------
# Talent pool (hiring managers, leaders, HR, admins)
# ---------------------------------------------------------------------------

@router.get("/talent")
def talent(request: Request, role: str = "", potential: str = "", user: dict = Depends(auth.require_role(*POOL))):
    smartrecruiters.ensure_loaded()
    roles = mobility.roles_in_demand()
    role = security.clean_form_text(role, 120) or next((r["role_id"] for r in roles if r["aspiring"] or r["applied"]), "")
    pool = mobility.talent_pool(role, user, include_potential=bool(potential)) if role else None
    if role and not pool:
        raise HTTPException(404, "Job profile not found")
    return render(request, "career/talent.html", roles=roles, pool=pool, selected=role, potential=bool(potential),
                  error=request.session.pop("flash_error", None), notice=pop_flash(request))


@router.get("/talent/{role_id}/{user_id}")
def talent_card(request: Request, role_id: str, user_id: str, user: dict = Depends(auth.require_role(*POOL))):
    role_id, user_id = security.clean_form_text(role_id, 120), security.clean_form_text(user_id, 80)
    card = mobility.talent_card(role_id, user_id, user)
    if not card:
        governance.record_audit(user["id"], user["role"], "talent_card", f"/talent/{role_id}/{user_id}", outcome="denied")
        raise HTTPException(404, "Candidate not found")
    governance.record_audit(user["id"], user["role"], "talent_card_viewed", f"/talent/{role_id}/{user_id}",
                            details={"opted_in": card["candidate"]["opted_in"]})
    return render(request, "career/talent_card.html", role=card["role"], c=card["candidate"],
                  can_open_profile=mobility.in_scope(user, user_id),
                  error=request.session.pop("flash_error", None), notice=pop_flash(request))


@router.post("/talent/{role_id}/{user_id}/shortlist")
def talent_shortlist(request: Request, role_id: str, user_id: str, note: Annotated[str, Form()] = "",
                     csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role(*POOL))):
    security.validate_csrf(request, csrf_token)
    role_id, user_id = security.clean_form_text(role_id, 120), security.clean_form_text(user_id, 80)
    try:
        mobility.shortlist(user, role_id, user_id, note)
    except mobility.MobilityError as exc:
        request.session["flash_error"] = str(exc)
        return RedirectResponse(f"/talent/{role_id}/{user_id}", status_code=303)
    governance.record_audit(user["id"], user["role"], "talent_shortlisted", f"/talent/{role_id}/{user_id}")
    flash(request, "Shortlisted. They have been told you would like to talk.")
    return RedirectResponse(f"/talent/{role_id}/{user_id}", status_code=303)


@router.post("/talent/applications/{application_id}/decision")
def talent_application_decision(request: Request, application_id: str, decision: Annotated[str, Form()] = "",
                                note: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "",
                                user: dict = Depends(auth.require_role(*POOL))):
    security.validate_csrf(request, csrf_token)
    try:
        application = mobility.decide_application(user, application_id, security.clean_form_text(decision, 20), note)
    except PermissionError:
        raise HTTPException(403, "Access restricted")
    except mobility.MobilityError as exc:
        request.session["flash_error"] = str(exc)
        application = store.get_application(security.clean_form_text(application_id, 40))
        if not application:
            raise HTTPException(404, "Application not found")
        return RedirectResponse(f"/talent/{application['role_id']}/{application['user_id']}", status_code=303)
    governance.record_audit(user["id"], user["role"], f"application_{decision}", f"/talent/applications/{application_id}")
    flash(request, "Application shortlisted." if decision == "shortlisted" else "Application declined; the applicant can read your note.")
    return RedirectResponse(f"/talent/{application['role_id']}/{application['user_id']}" if application["role_id"] else "/talent",
                            status_code=303)


# ---------------------------------------------------------------------------
# Internal jobs feed (SmartRecruiters) -- admins and HR partners
# ---------------------------------------------------------------------------

def jobs_page(request: Request, status_code: int = 200, error: str | None = None):
    return render(request, "org/jobs.html", status_code=status_code, postings=store.list_postings(status=None),
                  roles=mobility.role_options(), state=store.posting_sync_state(), connected=smartrecruiters.configured(),
                  company=smartrecruiters.settings()["company"], error=error or request.session.pop("flash_error", None),
                  notice=pop_flash(request))


@router.get("/org/jobs")
def org_jobs(request: Request, user: dict = Depends(auth.require_role(*HR_ADMIN))):
    smartrecruiters.ensure_loaded()
    return jobs_page(request)


@router.post("/org/jobs/sync")
def org_jobs_sync(request: Request, csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role(*HR_ADMIN))):
    security.validate_csrf(request, csrf_token)
    result = smartrecruiters.sync(force=True)
    governance.record_audit(user["id"], user["role"], "postings_synced", "/org/jobs/sync",
                            details={k: result[k] for k in ("source", "count", "mapped")})
    if result["error"]:
        request.session["flash_error"] = f"Sync failed: {result['error']} The last good copy is still shown."
    else:
        flash(request, f"{result['count']} open posting(s) from {'SmartRecruiters' if result['source'] == 'smartrecruiters' else 'the demo feed'}"
                       f"; {result['mapped']} matched a job profile by title.")
    return RedirectResponse("/org/jobs", status_code=303)


@router.post("/org/jobs/{posting_id}/map")
def org_job_map(request: Request, posting_id: str, role_id: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "",
                user: dict = Depends(auth.require_role(*HR_ADMIN))):
    security.validate_csrf(request, csrf_token)
    posting = store.get_posting(security.clean_form_text(posting_id, 80))
    role_id = security.clean_form_text(role_id, 120)
    if not posting:
        raise HTTPException(404, "Posting not found")
    if role_id and not progression.role_by_id(role_id):
        return jobs_page(request, status_code=400, error="Unknown job profile.")
    store.map_posting(posting["posting_id"], role_id or None, user["id"])
    governance.record_audit(user["id"], user["role"], "posting_mapped", f"/org/jobs/{posting['posting_id']}", details={"role_id": role_id})
    flash(request, f"“{posting['title']}” " + (f"mapped to {progression.role_by_id(role_id)['role_name']}." if role_id else "unmapped."))
    return RedirectResponse("/org/jobs", status_code=303)
