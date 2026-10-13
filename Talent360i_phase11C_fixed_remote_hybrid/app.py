"""Talent 360i server-rendered application with Phase 9 governance controls."""
import csv
import html
import io
import logging
import os
import secrets
import sqlite3
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from services import (api_client, auth, coding, db, engagement, insights, lifecycle_store, performance, probation,
                      progression, question_bank, schedule, security, tni)
from services.backend_api import BackendAPIError
from domain import demo_data, frameworks, a4_evidence, governance
from routes import career, hr, lifecycle_people, lifecycle_self, org_admin, platform
from routes.web import employee_scope, manager_scope, render

BASE_DIR = Path(__file__).resolve().parent
DEBUG_MODE = os.getenv("TALENT360_DEBUG", "true").lower() in {"1", "true", "yes", "development"}
SESSION_SECRET = os.getenv("TALENT360_SESSION_SECRET")
if not DEBUG_MODE and not SESSION_SECRET:
    raise RuntimeError("TALENT360_SESSION_SECRET must be configured when TALENT360_DEBUG is disabled")
if not SESSION_SECRET:
    # No hardcoded fallback secret (guideline A.7): a random per-process key
    # means sessions simply end when the dev server restarts.
    logging.getLogger("talent360i.portal").warning(
        "TALENT360_SESSION_SECRET is not set: using a random per-process key (debug mode only)."
    )
    SESSION_SECRET = secrets.token_urlsafe(48)

# debug is always False: Starlette's debug mode sends tracebacks to the
# browser and bypasses internal_error_handler below (guideline A.7).
app = FastAPI(title="Talent 360i", debug=False)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.middleware("http")
async def audit_writes(request: Request, call_next):
    response = await call_next(request)
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        user = auth.get_current_user(request)
        governance.record_audit(
            actor_id=user["id"] if user else "anonymous",
            actor_role=user["role"] if user else "anonymous",
            action=request.method,
            resource=request.url.path,
            outcome="success" if response.status_code < 400 else f"http_{response.status_code}",
        )
    return response


app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=not DEBUG_MODE,
    same_site="strict",
    max_age=3600,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if not request.url.path.startswith("/static/"):
        # Pages hold personal assessment data -- never cache them in the browser/proxies.
        response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; font-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if not DEBUG_MODE:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401:
        return auth.login_redirect()
    if exc.status_code == 403:
        return render(request, "errors/403.html", status_code=403)
    if exc.status_code == 404:
        return render(request, "errors/404.html", status_code=404)
    return HTMLResponse(html.escape(str(exc.detail)), status_code=exc.status_code)


@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    return render(request, "errors/404.html", status_code=404)


@app.exception_handler(Exception)
async def internal_error_handler(request: Request, exc: Exception):
    # Detail goes to the server log only; the user sees the generic 500 page.
    logging.getLogger("talent360i.portal").exception("Unhandled error on %s", request.url.path)
    return render(request, "errors/500.html", status_code=500)


# v3 employee lifecycle: people cards & 360 profiles, the employee's own
# performance page, job profiles / verticals / departments / skill catalogue /
# onboarding, the HR workspace, and platform routes (notifications, SME coding
# challenges, integrations, health).
for _module in (lifecycle_people, lifecycle_self, org_admin, hr, career, platform):
    app.include_router(_module.router)


@app.get("/", include_in_schema=False)
def home(request: Request):
    user = auth.get_current_user(request)
    return RedirectResponse(auth.landing_path(user), status_code=303) if user else auth.login_redirect()


@app.get("/login")
def login_page(request: Request):
    user = auth.get_current_user(request)
    return RedirectResponse(auth.landing_path(user), status_code=303) if user else render(request, "auth/login.html")


@app.post("/login")
def login(request: Request, username: Annotated[str, Form()] = "", password: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = ""):
    security.validate_csrf(request, csrf_token)
    username = security.clean_form_text(username, 60)
    if security.login_locked(request, username):
        governance.record_audit("anonymous", "anonymous", "login_locked", "/login", outcome="denied", details={"username": username})
        return render(request, "auth/login.html", status_code=429, error="Too many failed attempts. Try again in a few minutes.")
    user = auth.login(request, username, password[:128])
    if not user:
        security.record_login_failure(request, username)
        governance.record_audit("anonymous", "anonymous", "login_failed", "/login", outcome="denied", details={"username": username})
        return render(request, "auth/login.html", status_code=400, error="Incorrect username or password.")
    security.clear_login_failures(request, username)
    governance.record_audit(user["id"], user["role"], "login", "/login")
    return RedirectResponse(auth.landing_path(user), status_code=303)


@app.post("/logout")
def logout(request: Request, csrf_token: Annotated[str, Form()] = ""):
    security.validate_csrf(request, csrf_token)
    user = auth.get_current_user(request)
    if user:
        governance.record_audit(user["id"], user["role"], "logout", "/logout")
    request.session.clear()
    return auth.login_redirect()


# ---------------- Employee ----------------
def employee_dashboard_page(request: Request, user: dict, status_code: int = 200, upload_error: str | None = None):
    cycle = api_client.get_employee_cycle(employee_scope(user))
    decided = cycle and cycle["status"] in ("manager_approved", "manager_rejected")
    return render(request, "employee/dashboard.html", status_code=status_code, cycle=cycle, upload_error=upload_error,
                  certified=db.list_certifications(cycle_id=cycle["cycle_id"]) if decided else [],
                  outline=next(iter(db.list_outlines(cycle_id=cycle["cycle_id"])), None) if decided else None,
                  can_upload=not cycle or cycle["status"] not in question_bank.ACTIVE_STATUSES,
                  source_label=question_bank.SOURCE_LABELS.get((cycle or {}).get("match_status"), ""),
                  api_mode=os.getenv("TALENT360_API_MODE", "mock"))


@app.get("/employee/dashboard")
def employee_dashboard(request: Request, user: dict = Depends(auth.require_role("employee", "admin"))):
    # Auto-assigns an assessment built from pre-approved bank questions when the
    # employee has never been evaluated for their current role, was sent back to
    # retake, or finished their skill-gap training (idempotent; see question_bank).
    question_bank.ensure_baseline(employee_scope(user))
    return employee_dashboard_page(request, user)


@app.post("/employee/dashboard/upload")
async def employee_upload_resume(
    request: Request,
    resume: Annotated[UploadFile, File(...)] = None,
    csrf_token: Annotated[str, Form()] = "",
    user: dict = Depends(auth.require_role("employee", "admin")),
):
    # The employee only supplies the resume: the SME decides how many
    # questions, at what proficiency level, and which ones.
    if resume is None:
        raise HTTPException(status_code=400, detail="Please upload a resume file.")
    security.validate_csrf(request, csrf_token)
    uid = employee_scope(user)
    current = api_client.get_employee_cycle(uid)
    if current and current["status"] in question_bank.ACTIVE_STATUSES:
        return employee_dashboard_page(request, user, status_code=400, upload_error="You already have an assessment in progress.")
    if not api_client.backend_pipeline_enabled():
        return employee_dashboard_page(request, user, upload_error="Backend API mode is not enabled. Set TALENT360_API_MODE=remote and TALENT360_BACKEND_URL.")
    try:
        data = await resume.read(security.MAX_UPLOAD_BYTES + 1)
        safe_name = security.validate_upload(resume.filename, data)
    except ValueError as exc:
        return employee_dashboard_page(request, user, status_code=400, upload_error=str(exc))
    try:
        result = api_client.start_employee_cycle(uid, data, safe_name)
    except BackendAPIError as exc:
        # Only the adapter's own short, non-sensitive message is shown.
        return employee_dashboard_page(request, user, upload_error=str(exc))
    except Exception:
        logging.getLogger("uvicorn.error").exception("Resume upload failed")
        governance.record_audit(user["id"], user["role"], "resume_upload", "/employee/dashboard/upload", outcome="error")
        return employee_dashboard_page(request, user, upload_error="The resume could not be processed. Please try again or contact support.")
    if isinstance(result, dict) and result.get("cycle_id"):
        return RedirectResponse("/employee/dashboard", status_code=303)
    message = (result or {}).get("message", "The resume could not be matched to a role.")
    return employee_dashboard_page(request, user, upload_error=message)


@app.get("/employee/assessment/{cycle_id}")
def employee_assessment(request: Request, cycle_id: str, user: dict = Depends(auth.require_role("employee", "admin"))):
    cycle = api_client.get_employee_cycle(employee_scope(user))
    if not cycle or cycle["cycle_id"] != cycle_id or cycle["status"] != "ready_for_assessment":
        raise HTTPException(404, "Assessment not available")
    assessment = api_client.get_employee_takeable_assessment(cycle_id)
    return render(request, "employee/assessment.html", assessment=assessment)


@app.post("/employee/assessment/{cycle_id}/submit")
def employee_submit_assessment(request: Request, cycle_id: str, answers: Annotated[str | None, Form()] = None, csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role("employee", "admin"))):
    security.validate_csrf(request, csrf_token)
    cycle = api_client.get_employee_cycle(employee_scope(user))
    if not cycle or cycle["cycle_id"] != cycle_id or cycle["status"] != "ready_for_assessment":
        raise HTTPException(404, "Assessment not available")
    api_client.submit_employee_assessment(cycle_id, answers)
    # The score is never shown to the employee -- straight to the case-study round.
    return RedirectResponse(f"/employee/evidence/{cycle_id}", status_code=303)


def _simulation_cycle(user: dict, cycle_id: str) -> dict:
    cycle = api_client.get_employee_cycle(employee_scope(user))
    if not cycle or cycle["cycle_id"] != cycle_id or cycle["status"] != "mcq_submitted":
        raise HTTPException(404, "Simulation not available")
    return cycle


def simulation_page(request: Request, cycle_id: str, user: dict, status_code: int = 200, error: str | None = None):
    cycle = _simulation_cycle(user, cycle_id)
    try:
        api_client.get_employee_case_studies(cycle_id)
    except BackendAPIError:
        return render(request, "employee/evidence.html", status_code=503, cycle=cycle, sim=None,
                      error="The simulation could not be prepared right now. Please try again shortly.")
    cycle = api_client.get_employee_cycle(employee_scope(user))
    return render(request, "employee/evidence.html", status_code=status_code, cycle=cycle,
                  sim=api_client.get_simulation(cycle), coding_tasks=coding.challenges_for_cycle(cycle), error=error,
                  notice=request.session.pop("flash", None))


@app.get("/employee/evidence/{cycle_id}")
def employee_case_study(request: Request, cycle_id: str, user: dict = Depends(auth.require_role("employee", "admin"))):
    return simulation_page(request, cycle_id, user)


@app.post("/employee/evidence/{cycle_id}/respond")
def employee_simulation_respond(request: Request, cycle_id: str, question_id: Annotated[str, Form()] = "", response: Annotated[str, Form()] = "",
                                paste_chars: Annotated[str, Form()] = "", paste_events: Annotated[str, Form()] = "", active_seconds: Annotated[str, Form()] = "",
                                csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role("employee", "admin"))):
    security.validate_csrf(request, csrf_token)
    _simulation_cycle(user, cycle_id)
    text = security.clean_multiline_text(response, 3000)
    if len(text) < 2:
        return simulation_page(request, cycle_id, user, status_code=400, error="Write a response before sending it.")
    # Browser telemetry for the AI-use check; anything missing or malformed is simply not recorded.
    meta = {key: min(int(value), limit) for key, value, limit in (
        ("paste_chars", paste_chars, 100_000), ("paste_events", paste_events, 1_000), ("active_seconds", active_seconds, 86_400))
        if value.strip().isdigit()}
    result = api_client.respond_to_simulation(cycle_id, security.clean_form_text(question_id, 120), text, meta)
    if not result["ok"]:
        return simulation_page(request, cycle_id, user, status_code=400, error=result["reason"])
    return RedirectResponse(f"/employee/evidence/{cycle_id}#latest", status_code=303)


@app.post("/employee/evidence/{cycle_id}/finish-scenario")
def employee_simulation_finish(request: Request, cycle_id: str, question_id: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role("employee", "admin"))):
    security.validate_csrf(request, csrf_token)
    _simulation_cycle(user, cycle_id)
    if not api_client.finish_simulation_scenario(cycle_id, security.clean_form_text(question_id, 120)):
        return simulation_page(request, cycle_id, user, status_code=400, error="Answer the scenario at least once before finishing it.")
    return RedirectResponse(f"/employee/evidence/{cycle_id}#latest", status_code=303)


@app.post("/employee/evidence/{cycle_id}/submit")
def employee_submit_case_study(request: Request, cycle_id: str, csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role("employee", "admin"))):
    security.validate_csrf(request, csrf_token)
    cycle = _simulation_cycle(user, cycle_id)
    if coding.pending_for_cycle(cycle):
        return simulation_page(request, cycle_id, user, status_code=400, error="Submit your coding task before the simulation.")
    # Always reaches the manager once every scenario is finished; if Agent 4 is
    # unavailable its report follows in the background (see submit_employee_case_study).
    submitted = api_client.submit_employee_case_study(cycle_id)
    if submitted is None:
        return simulation_page(request, cycle_id, user, status_code=400, error="Finish every scenario before submitting.")
    governance.record_audit(user["id"], user["role"], "simulation_submitted", f"/employee/evidence/{cycle_id}",
                            details={"evidence_pending": submitted["evidence_pending"]})
    return RedirectResponse("/employee/dashboard", status_code=303)


@app.get("/employee/learning")
def employee_learning(request: Request, user: dict = Depends(auth.require_role("employee", "admin"))):
    # The AI (Agent 5) plan still only appears once the manager approves the
    # cycle; the TNI recommendations are the dataset's own gap-to-course
    # mapping and the schedule is what the scheduler booked, so neither waits.
    uid = employee_scope(user)
    cycle = api_client.get_employee_cycle(uid)
    plan = cycle.get("dev_plan") if cycle and cycle["status"] == "manager_approved" else None
    upcoming = schedule.upcoming_for_user(uid, date.today())
    return render(request, "employee/learning.html", plan=plan, cycle=cycle, tni=tni.employee_tni(uid), upcoming=upcoming,
                  booked={e["course_id"]: e for e in schedule.events_for_user(uid) if e["course_id"]},
                  outlines=progression.outlines_for([uid]))


@app.get("/employee/skills")
def employee_skills(request: Request, status: str = "All", user: dict = Depends(auth.require_role("employee", "admin"))):
    status = security.clean_form_text(status, 40)
    uid = employee_scope(user)
    profile = tni.employee_tni(uid)
    if profile:
        skills = [s for s in profile["skills"] if status == "All" or s["status"] == status]
        nomination = next(iter(db.list_nominations(user_ids=[uid])), None)
        return render(request, "employee/skills.html", tni=profile, skills=skills, selected_status=status,
                      career=progression.readiness(uid), nomination=nomination)
    return render(request, "employee/skills.html", tni=None, skills=api_client.get_employee_skills(uid, status), summary=api_client.get_skill_summary(uid), selected_status=status)


@app.get("/employee/assessments")
def employee_assessments(request: Request, user: dict = Depends(auth.require_role("employee", "admin"))):
    rows = api_client.get_employee_assessment_history(employee_scope(user), date.today())
    counts = {group: sum(1 for r in rows if r["group"] == group) for group in ("Pending", "In progress", "Completed", "Scheduled")}
    return render(request, "employee/assessments.html", assessments=rows, counts=counts)


@app.get("/employee/calendar")
def employee_calendar(request: Request, month: str = "", kind: str = "all", user: dict = Depends(auth.require_role("employee", "admin"))):
    today = date.today()
    uid = employee_scope(user)
    return render(request, "employee/calendar.html",
                  cal=schedule.month_view(schedule.parse_month(month, today), today, security.clean_form_text(kind, 20), user_id=uid),
                  upcoming=schedule.upcoming_for_user(uid, today)[:5])


def _ics_response(text: str, filename: str) -> Response:
    return Response(text, media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/employee/calendar.ics")
def employee_calendar_ics(request: Request, user: dict = Depends(auth.require_role("employee", "admin"))):
    uid = employee_scope(user)
    return _ics_response(schedule.to_ics(schedule.events_for_user(uid), "Talent 360i · my learning", show_who=False),
                         "talent360i-my-calendar.ics")


@app.get("/learning/catalog/{training_id}")
def learning_catalog_item(request: Request, training_id: str, user: dict = Depends(auth.require_role("employee", "manager", "leader", "admin", "hr"))):
    from agents.a5_development import TRAINING_CATALOGUE
    item = next((x for x in TRAINING_CATALOGUE if x["id"] == training_id), None)
    if not item:
        # Real catalogue courses (TRN-00xx) cited by TNI recommendations and A5 plans.
        course = tni.get_course(security.clean_form_text(training_id, 40))
        if not course:
            raise HTTPException(404, "Learning item not found")
        item = {
            "id": course["course_id"], "title": course["course_title"],
            "description": f"{course['delivery_type']} · level group: {course['level_group']} · risk: {course['risk_level'] or '—'} · owner: {course['spoc_role'] or '—'}.",
            "skill": ", ".join(course["skills"]) or "—", "framework": course["tower"] or "—",
            "type": course["delivery_type"], "duration": course["level_group"],
        }
    return render(request, "learning/resource.html", resource_type="Learning", resource=item)


@app.get("/knowledge/sops/{sop_id}")
def curated_sop(request: Request, sop_id: str, user: dict = Depends(auth.require_role("employee", "manager", "admin"))):
    from agents.a5_development import CURATED_SOP_LINKS
    item = CURATED_SOP_LINKS.get(sop_id)
    if not item:
        raise HTTPException(404, "SOP reference not found")
    resource = {"id": sop_id, "title": item["title"], "deep_link": item["deep_link"], "description": "Synthetic curated SOP reference for the Talent 360i development build. Replace with the approved internal SOP deep link in production."}
    return render(request, "learning/resource.html", resource_type="Curated SOP", resource=resource)


# ---------------- Manager ----------------
@app.get("/manager/validations")
def manager_validations(request: Request, user: dict = Depends(auth.require_role("manager", "admin"))):
    cycles = api_client.get_manager_cycle_queue(user["id"])
    outcomes = {c["cycle_id"]: progression.assessment_outcome(db.get_cycle(c["cycle_id"])) for c in cycles}
    scope = None if user["role"] == "admin" else user["id"]
    return render(request, "manager/validations.html", cycles=cycles, outcomes=outcomes,
                  pending_evidence={c["cycle_id"] for c in cycles if api_client.evidence_pending(c)},
                  pipeline=api_client.manager_assessment_pipeline(scope) if scope else {"in_progress": [], "decided": []},
                  notice=request.session.pop("flash", None))


def validation_page(request: Request, cycle: dict, status_code: int = 200, error: str | None = None):
    awaiting = cycle["status"] == "pending_manager_approval"
    decided = cycle["status"] in ("manager_approved", "manager_rejected")
    dev_plan = dev_plan_error = None
    if awaiting or decided:  # a plan only means something once the simulation is in
        try:
            dev_plan = api_client.generate_employee_dev_plan(cycle["cycle_id"])
        except BackendAPIError as exc:
            dev_plan_error = str(exc)
    reviews = api_client.get_calibration_reviews(cycle["cycle_id"])
    rereview = reviews[0] if reviews and reviews[0]["outcome"] == "rereview" and awaiting else None
    current = tni.current_role(cycle["user_id"])
    return render(
        request, "manager/validation_detail.html", status_code=status_code, cycle=cycle, dev_plan=dev_plan,
        dev_plan_error=dev_plan_error, governance_notes=api_client.get_cycle_governance_notes(cycle["cycle_id"]),
        rereview=rereview, outcome=progression.assessment_outcome(cycle), kind=progression.assessment_kind(cycle, current),
        current_role=current, next_role=progression.next_role(cycle["matched_role_id"]), error=error,
        coding_attempts=lifecycle_store.list_attempts(cycle_id=cycle["cycle_id"]),
        awaiting=awaiting, decided=decided, evidence_pending=api_client.evidence_pending(cycle),
        stage=None if awaiting or decided else api_client.assessment_stage(cycle),
        conversations=api_client.simulation_conversations(cycle), notice=request.session.pop("flash", None),
    )


def _queue_cycle(user: dict, cycle_id: str, action: str, denied_status: int = 404) -> dict:
    cycle_id = security.clean_form_text(cycle_id, 80)
    cycle = api_client.get_manager_cycle_detail(cycle_id)
    # Resource-level authorization (broken access control): a manager may only
    # view or decide cycles belonging to their own direct reports. Admins are exempt.
    if not cycle or (user["role"] != "admin" and cycle.get("manager_id") != user["id"]):
        governance.record_audit(user["id"], user["role"], action, f"/manager/validations/{cycle_id}", outcome="denied")
        raise HTTPException(denied_status, "Cycle not found" if denied_status == 404 else "Access restricted")
    return cycle


@app.get("/manager/validations/{cycle_id}")
def manager_validation_detail(request: Request, cycle_id: str, user: dict = Depends(auth.require_role("manager", "admin"))):
    return validation_page(request, _queue_cycle(user, cycle_id, "manager_view_cycle"))


@app.post("/manager/validations/{cycle_id}/evidence")
def manager_retry_evidence(request: Request, cycle_id: str, csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role("manager", "admin"))):
    security.validate_csrf(request, csrf_token)
    cycle = _queue_cycle(user, cycle_id, "manager_retry_evidence", denied_status=403)
    if api_client.retry_evidence_report(cycle["cycle_id"]):
        request.session["flash"] = "Agent 4's evidence report is ready below."
    else:
        request.session["flash"] = ("Agent 4 is still unavailable. The report keeps retrying in the background; you can "
                                    "also decide from the MCQ and the conversation, with a short rationale.")
    governance.record_audit(user["id"], user["role"], "evidence_retry", f"/manager/validations/{cycle['cycle_id']}")
    return RedirectResponse(f"/manager/validations/{cycle['cycle_id']}", status_code=303)


@app.post("/manager/validations/{cycle_id}/decision")
def manager_validation_decision(request: Request, cycle_id: str, decision: Annotated[str, Form()] = "", note: Annotated[str | None, Form()] = None, certify: Annotated[list[str], Form()] = [], csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role("manager", "admin"))):  # noqa: B006 -- FastAPI form default
    security.validate_csrf(request, csrf_token)
    decision = security.clean_form_text(decision, 30)
    if decision not in progression.DECISIONS:
        raise HTTPException(400, "Validation action could not be completed.")
    cycle = _queue_cycle(user, cycle_id, "manager_decision", denied_status=403)
    if cycle["status"] != "pending_manager_approval":
        raise HTTPException(400, "Validation action could not be completed.")
    try:
        result = progression.decide(cycle["cycle_id"], decision, security.clean_form_text(note or "", 2000), user["id"],
                                    certify=[security.clean_form_text(c, 200) for c in certify[:40]])
    except progression.DecisionError as exc:
        return validation_page(request, cycle, status_code=400, error=str(exc))
    governance.record_audit(user["id"], user["role"], f"manager_{decision}", f"/manager/validations/{cycle['cycle_id']}",
                            details={"certified": result["certified"], "outline_sessions": result["sessions"]})
    performance.refresh([cycle["user_id"]])  # the decision feeds the on/off-track status straight away
    name = cycle["employee_name"]
    if decision == "approved":
        msg = f"Approved {name}: {result['certified']} skill level(s) certified" + (
            f", {result['gaps']} gap(s) recorded." if result["gaps"] else ".")
    elif decision == "rejected":
        msg = (f"Rejected {name}'s assessment: {result['gaps']} confirmed skill gap(s) recorded on their profile "
               "and skill-gap training assigned.")
    else:
        msg = f"Sent {name}'s assessment back: a retake is assigned from the question bank."
    if result["outline_id"]:
        msg += (f" Agent 5 outlined {result['sessions']} training session(s), booked on {name}'s calendar."
                if result["outline_ready"] else " The training outline is being prepared and will be booked shortly.")
    request.session["flash"] = msg
    return RedirectResponse("/manager/validations", status_code=303)


@app.get("/manager/dashboard")
def manager_dashboard(request: Request, user: dict = Depends(auth.require_role("manager", "admin"))):
    manager_id = manager_scope(user)
    today = date.today()
    views = api_client.get_manager_team_views(manager_id)
    cards = performance.team_cards(manager_id, today)
    team_ids = [c["user_id"] for c in cards]
    return render(request, "manager/dashboard.html", dashboard=views["dashboard"], matrix=views["matrix"],
                  cards=cards, bands=performance.band_summary(cards),
                  pending_evidence=lifecycle_store.pending_evidence(team_ids),
                  escalations=lifecycle_store.list_escalations(user_ids=team_ids),
                  probation=probation.pipeline(set(team_ids), today), pulse=engagement.team_pulse(team_ids, today),
                  notice=request.session.pop("flash", None))


def manager_page(request: Request, template: str, title: str, user: dict):
    return render(request, template, page_title=title, **api_client.get_manager_team_views(manager_scope(user)))


@app.get("/manager/team-skills")
def manager_team_skills(request: Request, user: dict = Depends(auth.require_role("manager", "admin"))): return manager_page(request, "manager/team_skills.html", "Team Skills", user)
@app.get("/manager/skill-matrix")
def manager_skill_matrix(request: Request, user: dict = Depends(auth.require_role("manager", "admin"))): return manager_page(request, "manager/skill_matrix.html", "Team Skill Matrix", user)
@app.get("/manager/assessments")
def manager_assessments(request: Request, user: dict = Depends(auth.require_role("manager", "admin"))): return manager_page(request, "manager/assessments.html", "Team Assessments", user)
@app.get("/manager/skill-gaps")
def manager_skill_gaps(request: Request, user: dict = Depends(auth.require_role("manager", "admin"))): return manager_page(request, "manager/skill_gaps.html", "Team Skill Gaps", user)
@app.get("/manager/learning")
def manager_learning(request: Request, role: str = "", user: dict = Depends(auth.require_role("manager", "admin"))):
    manager_id = manager_scope(user)
    return render(request, "manager/learning.html", page_title="Learning & TNI",
                  team_tni=tni.team_tni(manager_id, security.clean_form_text(role, 120) or None),
                  development_plans=api_client.get_manager_development_plans(manager_id))
def _team_ids(manager_id: str) -> set[str]:
    return {e["user_id"] for e in db.list_employees() if e["manager_id"] == manager_id}


def training_page(request: Request, user: dict, employee: str = "", status_code: int = 200, error: str | None = None):
    today = date.today()
    manager_id = manager_scope(user)
    team = tni.team_tni(manager_id)
    people = team["people"]
    selected = next((p for p in people if p["user_id"] == employee), None) or next((p for p in people if p["path"]), None)
    enrolments = schedule.team_training(_team_ids(manager_id), today)
    booked = {e["course_id"]: e for e in enrolments if selected and e["user_id"] == selected["user_id"] and e["course_id"]}
    return render(request, "manager/training.html", status_code=status_code, people=people, selected=selected,
                  enrolments=enrolments, booked=booked, today=today.isoformat(), error=error,
                  outlines=progression.outlines_for(sorted(_team_ids(manager_id)), today),
                  notice=request.session.pop("flash", None))


def _own_report(user: dict, employee_id: str) -> str:
    employee_id = security.clean_form_text(employee_id, 80)
    if employee_id not in _team_ids(manager_scope(user)):
        governance.record_audit(user["id"], user["role"], "training_assign", "/manager/training", outcome="denied",
                                details={"employee_id": employee_id})
        raise HTTPException(403, "Access restricted")
    return employee_id


@app.get("/manager/training")
def manager_training(request: Request, employee: str = "", user: dict = Depends(auth.require_role("manager", "admin"))):
    return training_page(request, user, security.clean_form_text(employee, 80))


@app.post("/manager/training/enroll")
def manager_training_enroll(request: Request, employee_id: Annotated[str, Form()] = "", course_id: Annotated[str, Form()] = "", event_date: Annotated[str, Form()] = "", start_time: Annotated[str, Form()] = "10:00", csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role("manager", "admin"))):
    security.validate_csrf(request, csrf_token)
    employee_id = _own_report(user, employee_id)
    try:
        day = schedule.parse_date(event_date)
        if day < date.today():
            raise ValueError("Choose today or a future date.")
        schedule.book_training(user_id=employee_id, course_id=security.clean_form_text(course_id, 40), event_date=day,
                               start_time=schedule.parse_time(start_time), duration=60, created_by=user["id"],
                               note_prefix="Assigned by manager · ")
    except ValueError as exc:
        return training_page(request, user, employee_id, status_code=400, error=str(exc))
    governance.record_audit(user["id"], user["role"], "training_assigned", "/manager/training/enroll", details={"employee_id": employee_id})
    request.session["flash"] = f"Enrolled {employee_id} — session on {day.strftime('%a %d %b %Y')}."
    return RedirectResponse(f"/manager/training?employee={employee_id}", status_code=303)


@app.post("/manager/training/enroll-all")
def manager_training_enroll_all(request: Request, employee_id: Annotated[str, Form()] = "", start_date: Annotated[str, Form()] = "", cadence_days: Annotated[int, Form()] = 7, csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role("manager", "admin"))):
    security.validate_csrf(request, csrf_token)
    employee_id = _own_report(user, employee_id)
    try:
        start = schedule.parse_date(start_date)
        if start < date.today():
            raise ValueError("Choose today or a future start date.")
    except ValueError as exc:
        return training_page(request, user, employee_id, status_code=400, error=str(exc))
    result = schedule.auto_schedule_training(
        user_ids=[employee_id], start=start, per_employee=10, cadence_days=14 if cadence_days == 14 else 7,
        start_time="10:00", duration=60, created_by=user["id"], note_prefix="Assigned by manager · ")
    governance.record_audit(user["id"], user["role"], "training_assigned", "/manager/training/enroll-all",
                            details={"employee_id": employee_id, "created": result["created"]})
    request.session["flash"] = (f"Enrolled {employee_id} in {result['created']} recommended course(s)." if result["created"]
                                else "Nothing new to enrol: every recommended course is already booked.")
    return RedirectResponse(f"/manager/training?employee={employee_id}", status_code=303)


@app.post("/manager/training/{event_id}/{action}")
def manager_training_update(request: Request, event_id: str, action: str, csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role("manager", "admin"))):
    security.validate_csrf(request, csrf_token)
    if action not in {"complete", "delivered", "withdraw"}:
        raise HTTPException(404, "Not found")
    event = db.get_event(security.clean_form_text(event_id, 40))
    if not event or event["event_type"] != "training":
        raise HTTPException(404, "Booking not found")
    employee_id = _own_report(user, event["user_id"] or "")
    handler = {"complete": schedule.complete_training, "delivered": schedule.mark_delivered,
               "withdraw": schedule.cancel_event}[action]
    if not handler(event["event_id"], date.today()):
        raise HTTPException(400, "That booking can't be updated.")
    governance.record_audit(user["id"], user["role"], f"training_{action}", f"/manager/training/{event['event_id']}")
    request.session["flash"] = "Enrolment withdrawn." if action == "withdraw" else "Marked as completed."
    return RedirectResponse(f"/manager/training?employee={employee_id}", status_code=303)


@app.get("/manager/reports")
def manager_reports(request: Request, user: dict = Depends(auth.require_role("manager", "admin"))): return manager_page(request, "manager/reports.html", "Team Reports", user)


# ---------------- Scheduler ----------------
def _flash(request: Request, message: str):
    request.session["flash"] = message


def schedule_page(request: Request, month: str = "", kind: str = "all", status_code: int = 200, error: str | None = None):
    today = date.today()
    return render(
        request, "scheduler/schedule.html", status_code=status_code,
        cal=schedule.month_view(schedule.parse_month(month, today), today, security.clean_form_text(kind, 20)),
        today=today.isoformat(), employees=schedule.employee_options(), training_options=schedule.training_options(),
        error=error, notice=request.session.pop("flash", None), **api_client.get_scheduler_schedule(),
    )


def _month_of(day: date) -> str:
    return day.strftime("%Y-%m")


@app.get("/scheduler/dashboard")
def scheduler_dashboard(request: Request, user: dict = Depends(auth.require_role("scheduler", "sme", "admin"))):
    today = date.today()
    dashboard = api_client.get_scheduler_dashboard()
    counts = schedule.month_view(today.replace(day=1), today)["counts"]
    dashboard["metrics"]["scheduled"] = counts["assessment"] + counts["training"]
    return render(request, "scheduler/dashboard.html", dashboard=dashboard)

@app.get("/scheduler/blueprint")
def scheduler_blueprint(request: Request, user: dict = Depends(auth.require_role("scheduler", "sme", "admin"))):
    return render(request, "scheduler/blueprint.html", blueprint=demo_data.BLUEPRINT, framework=frameworks.get_framework("finops"))

@app.get("/scheduler/schedule")
def scheduler_schedule(request: Request, month: str = "", kind: str = "all", user: dict = Depends(auth.require_role("scheduler", "sme", "admin"))):
    return schedule_page(request, month, kind)


@app.get("/scheduler/schedule.ics")
def scheduler_schedule_ics(request: Request, month: str = "", user: dict = Depends(auth.require_role("scheduler", "sme", "admin"))):
    start = schedule.parse_month(month, date.today())
    governance.record_audit(user["id"], user["role"], "calendar_exported", "/scheduler/schedule.ics", details={"month": start.strftime("%Y-%m")})
    return _ics_response(schedule.to_ics(schedule.month_events(start), f"Talent 360i · {start.strftime('%B %Y')}"),
                         f"talent360i-schedule-{start.strftime('%Y-%m')}.ics")


@app.post("/scheduler/schedule/events")
def scheduler_book_event(
    request: Request,
    event_type: Annotated[str, Form()] = "",
    employee_id: Annotated[str, Form()] = "",
    training_choice: Annotated[str, Form()] = "",
    event_date: Annotated[str, Form()] = "",
    start_time: Annotated[str, Form()] = "",
    duration: Annotated[int, Form()] = 60,
    csrf_token: Annotated[str, Form()] = "",
    user: dict = Depends(auth.require_role("scheduler", "sme", "admin")),
):
    security.validate_csrf(request, csrf_token)
    duration = max(5, min(480, duration))
    try:
        day = schedule.parse_date(event_date)
        if day < date.today():
            raise ValueError("Choose today or a future date.")
        time = schedule.parse_time(start_time)
        if event_type == "assessment":
            schedule.book_assessment(user_id=security.clean_form_text(employee_id, 80), event_date=day,
                                     start_time=time, duration=duration, created_by=user["id"])
        elif event_type == "training":
            employee, _, course_id = security.clean_form_text(training_choice, 200).partition("|")
            schedule.book_training(user_id=employee, course_id=course_id, event_date=day,
                                   start_time=time, duration=duration, created_by=user["id"])
        else:
            raise ValueError("Choose assessment or training.")
    except ValueError as exc:
        return schedule_page(request, status_code=400, error=str(exc))
    _flash(request, f"{event_type.title()} booked for {day.strftime('%a %d %b %Y')}.")
    return RedirectResponse(f"/scheduler/schedule?month={_month_of(day)}", status_code=303)


@app.post("/scheduler/schedule/auto-training")
def scheduler_auto_training(
    request: Request,
    scope: Annotated[str, Form()] = "all",
    start_date: Annotated[str, Form()] = "",
    per_employee: Annotated[int, Form()] = 2,
    cadence_days: Annotated[int, Form()] = 7,
    start_time: Annotated[str, Form()] = "10:00",
    duration: Annotated[int, Form()] = 60,
    csrf_token: Annotated[str, Form()] = "",
    user: dict = Depends(auth.require_role("scheduler", "sme", "admin")),
):
    security.validate_csrf(request, csrf_token)
    scope = security.clean_form_text(scope, 80)
    employee_ids = [e["id"] for e in schedule.employee_options()]
    if scope != "all" and scope not in employee_ids:
        return schedule_page(request, status_code=400, error="Choose an employee or the whole cohort.")
    try:
        start = schedule.parse_date(start_date)
        if start < date.today():
            raise ValueError("Choose today or a future start date.")
        result = schedule.auto_schedule_training(
            user_ids=employee_ids if scope == "all" else [scope], start=start,
            per_employee=max(1, min(5, per_employee)), cadence_days=7 if cadence_days not in (7, 14) else cadence_days,
            start_time=schedule.parse_time(start_time), duration=max(15, min(480, duration)), created_by=user["id"],
        )
    except ValueError as exc:
        return schedule_page(request, status_code=400, error=str(exc))
    governance.record_audit(user["id"], user["role"], "training_auto_scheduled", "/scheduler/schedule/auto-training",
                            details={"scope": scope, "created": result["created"]})
    if result["created"]:
        _flash(request, f"Booked {result['created']} training session(s) for {result['people']} employee(s) from their TNI skill-up path.")
    else:
        _flash(request, "Nothing new to book: every recommended course is already scheduled (or there are no open gaps).")
    return RedirectResponse(f"/scheduler/schedule?month={_month_of(schedule.add_business_days(start, 0))}", status_code=303)


@app.post("/scheduler/schedule/events/{event_id}/cancel")
def scheduler_cancel_event(request: Request, event_id: str, month: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role("scheduler", "sme", "admin"))):
    security.validate_csrf(request, csrf_token)
    if not schedule.cancel_event(security.clean_form_text(event_id, 40), date.today()):
        raise HTTPException(404, "Booking not found")
    _flash(request, "Booking cancelled.")
    return RedirectResponse(f"/scheduler/schedule?month={schedule.parse_month(month, date.today()).strftime('%Y-%m')}", status_code=303)


@app.post("/scheduler/schedule/launch")
def scheduler_launch(request: Request, due_date: Annotated[str, Form()] = "", duration: Annotated[int, Form()] = 30, csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role("scheduler", "sme", "admin"))):
    security.validate_csrf(request, csrf_token)
    try:
        due = schedule.parse_date(due_date)
    except ValueError as exc:
        return schedule_page(request, status_code=400, error=str(exc))
    duration = max(5, min(180, int(duration)))
    result = api_client.launch_schedule(due_date=due.strftime("%d %b %Y"), duration=duration, due_iso=due.isoformat())
    if not result["ok"]:
        return schedule_page(request, status_code=400, error=result["reason"])
    if result.get("created"):
        launched = result["schedule"]
        schedule.book_cohort_assessment(title=launched["name"], event_date=due, duration=duration, created_by=user["id"])
    return RedirectResponse(f"/scheduler/schedule?month={_month_of(due)}", status_code=303)

@app.get("/scheduler/assignments")
def scheduler_assignments(request: Request, user: dict = Depends(auth.require_role("scheduler", "sme", "admin"))):
    return render(request, "scheduler/assignments.html", assignments=api_client.get_scheduler_assignments(), schedules=api_client.get_schedules(), users=schedule.employee_options())

@app.post("/scheduler/assignments/{schedule_id}/assign")
def scheduler_assign(request: Request, schedule_id: str, employee_id: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role("scheduler", "sme", "admin"))):
    security.validate_csrf(request, csrf_token)
    result = api_client.assign_assessment(security.clean_form_text(schedule_id, 80), security.clean_form_text(employee_id, 80))
    if not result["ok"]:
        raise HTTPException(400, result["reason"])
    assignment = result["assignment"]
    if result.get("created") and assignment.get("due_iso"):
        schedule.book_assessment(user_id=assignment["employee_id"], event_date=date.fromisoformat(assignment["due_iso"]),
                                 start_time=None, duration=assignment.get("duration") or 30, created_by=user["id"],
                                 title=assignment["name"])
    return RedirectResponse("/scheduler/assignments", status_code=303)


# ---------------- Leader ----------------
def leader_framework(framework: str) -> str:
    return framework if framework in {"finops", "rd"} else "finops"

@app.get("/leader/dashboard")
def leader_dashboard(request: Request, framework: str = "finops", user: dict = Depends(auth.require_role("leader", "admin"))):
    framework = leader_framework(framework)
    return render(request, "leader/dashboard.html", distribution=insights.distribution(framework), selected_framework=framework, leader=insights.leader_dashboard(date.today()))

@app.get("/leader/reports")
def leader_reports(request: Request, user: dict = Depends(auth.require_role("leader", "admin"))):
    return render(request, "leader/reports.html", report=insights.org_report(date.today()))


def _csv_cell(value) -> str:
    # CSV/formula injection: a cell a spreadsheet would evaluate is forced to text.
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@app.get("/leader/reports/export.csv")
def leader_reports_export(request: Request, user: dict = Depends(auth.require_role("leader", "admin"))):
    rows = insights.org_export_rows(date.today())
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    columns = list(rows[0].keys()) if rows else ["employee_id"]
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_cell(row[c]) for c in columns])
    governance.record_audit(user["id"], user["role"], "org_report_exported", "/leader/reports/export.csv", details={"rows": len(rows)})
    return Response(buffer.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="talent360i-org-report-{date.today().isoformat()}.csv"'})

@app.get("/leader/distribution")
def leader_distribution(request: Request, framework: str = "finops", user: dict = Depends(auth.require_role("leader", "admin"))):
    framework = leader_framework(framework)
    return render(request, "leader/distribution.html", framework_id=framework, insights=insights.framework_insights(framework))

@app.get("/leader/critical-skills")
def leader_critical_skills(request: Request, framework: str = "finops", user: dict = Depends(auth.require_role("leader", "admin"))):
    framework = leader_framework(framework)
    return render(request, "leader/critical_skills.html", framework_id=framework, coverage=insights.critical_coverage(framework))

@app.get("/leader/movement")
def leader_movement(request: Request, framework: str = "finops", user: dict = Depends(auth.require_role("leader", "admin"))):
    framework = leader_framework(framework)
    return render(request, "leader/movement.html", framework_id=framework, movement=api_client.get_leader_movement(framework))

@app.get("/leader/item-analysis")
def leader_item_analysis(request: Request, user: dict = Depends(auth.require_role("leader", "admin"))):
    return render(request, "leader/item_analysis.html", items=api_client.get_leader_item_analysis())

# ---------------- Promotion readiness (manager -> leader) ----------------
def _team_row(user: dict, employee_id: str) -> dict:
    employee_id = security.clean_form_text(employee_id, 80)
    row = next((r for r in progression.team_promotions(manager_scope(user)) if r["user_id"] == employee_id), None)
    if not row:
        governance.record_audit(user["id"], user["role"], "promotion_view", f"/manager/promotions/{employee_id}", outcome="denied")
        raise HTTPException(404, "Employee not found")
    return row


@app.get("/manager/promotions")
def manager_promotions(request: Request, user: dict = Depends(auth.require_role("manager", "admin"))):
    rows = progression.team_promotions(manager_scope(user))
    return render(request, "manager/promotions.html", rows=rows, summary=progression.readiness_summary(rows),
                  notice=request.session.pop("flash", None))


def promotion_person_page(request: Request, user: dict, employee_id: str, status_code: int = 200, error: str | None = None):
    row = _team_row(user, employee_id)
    return render(request, "manager/promotion_detail.html", status_code=status_code, row=row, error=error,
                  profile=progression.nomination_profile(row["user_id"]), notice=request.session.pop("flash", None))


@app.get("/manager/promotions/{employee_id}")
def manager_promotion_person(request: Request, employee_id: str, user: dict = Depends(auth.require_role("manager", "admin"))):
    return promotion_person_page(request, user, employee_id)


@app.post("/manager/promotions/{employee_id}/assess")
def manager_promotion_assess(request: Request, employee_id: str, csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role("manager", "admin"))):
    security.validate_csrf(request, csrf_token)
    row = _team_row(user, employee_id)
    try:
        cycle_id = progression.request_promotion_assessment(row["user_id"], user["id"])
    except progression.DecisionError as exc:
        return promotion_person_page(request, user, row["user_id"], status_code=400, error=str(exc))
    governance.record_audit(user["id"], user["role"], "promotion_assessment_requested", f"/manager/promotions/{row['user_id']}",
                            details={"cycle_id": cycle_id, "role_id": row["next"]["role_id"]})
    request.session["flash"] = (f"Promotion-readiness assessment for {row['next']['role_name']} assigned to {row['name']}, "
                                "built from SME-approved bank questions.")
    return RedirectResponse(f"/manager/promotions/{row['user_id']}", status_code=303)


@app.post("/manager/promotions/{employee_id}/nominate")
def manager_promotion_nominate(request: Request, employee_id: str, note: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role("manager", "admin"))):
    security.validate_csrf(request, csrf_token)
    row = _team_row(user, employee_id)
    try:
        nomination_id = progression.nominate(row["user_id"], user["id"], security.clean_form_text(note, 2000))
    except progression.DecisionError as exc:
        return promotion_person_page(request, user, row["user_id"], status_code=400, error=str(exc))
    governance.record_audit(user["id"], user["role"], "promotion_nominated", f"/manager/promotions/{row['user_id']}",
                            details={"nomination_id": nomination_id})
    request.session["flash"] = f"{row['name']}'s profile was sent to the leadership team for promotion review."
    return RedirectResponse(f"/manager/promotions/{row['user_id']}", status_code=303)


@app.get("/leader/promotions")
def leader_promotions(request: Request, user: dict = Depends(auth.require_role("leader", "admin"))):
    rows = progression.team_promotions(None)
    return render(request, "leader/promotions.html", rows=rows, summary=progression.readiness_summary(rows),
                  nominations=db.list_nominations(), notice=request.session.pop("flash", None))


def nomination_page(request: Request, nomination_id: str, status_code: int = 200, error: str | None = None):
    nomination_id = security.clean_form_text(nomination_id, 40)
    nomination = next((n for n in db.list_nominations() if n["nomination_id"] == nomination_id), None)
    if not nomination:
        raise HTTPException(404, "Nomination not found")
    return render(request, "leader/promotion_detail.html", status_code=status_code, nomination=nomination,
                  profile=nomination["profile"], live=progression.readiness(nomination["user_id"]), error=error)


@app.get("/leader/promotions/{nomination_id}")
def leader_promotion_detail(request: Request, nomination_id: str, user: dict = Depends(auth.require_role("leader", "admin"))):
    return nomination_page(request, nomination_id)


@app.post("/leader/promotions/{nomination_id}/decision")
def leader_promotion_decision(request: Request, nomination_id: str, decision: Annotated[str, Form()] = "", note: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role("leader", "admin"))):
    security.validate_csrf(request, csrf_token)
    nomination_id = security.clean_form_text(nomination_id, 40)
    decision = security.clean_form_text(decision, 20)
    try:
        progression.decide_nomination(nomination_id, user["id"], decision, security.clean_form_text(note, 2000))
    except progression.DecisionError as exc:
        return nomination_page(request, nomination_id, status_code=400, error=str(exc))
    governance.record_audit(user["id"], user["role"], f"promotion_{decision}", f"/leader/promotions/{nomination_id}")
    request.session["flash"] = ("Promotion approved and recorded for HR." if decision == "approved"
                                else "Nomination declined; the manager can see your note.")
    return RedirectResponse("/leader/promotions", status_code=303)


@app.get("/leader/calibration")
def leader_calibration(request: Request, manager: str = "", user: dict = Depends(auth.require_role("leader", "admin"))):
    return render(request, "leader/calibration.html", calibration=insights.calibration(security.clean_form_text(manager, 120) or None),
                  manager_filter=security.clean_form_text(manager, 120), notice=request.session.pop("flash", None))


def _decided_cycle(cycle_id: str) -> dict:
    cycle = api_client.get_manager_cycle_detail(security.clean_form_text(cycle_id, 80))
    if not cycle or not cycle.get("manager_decision"):
        raise HTTPException(404, "Decision not found")
    return cycle

@app.get("/leader/calibration/{cycle_id}")
def leader_calibration_detail(request: Request, cycle_id: str, user: dict = Depends(auth.require_role("leader", "admin"))):
    cycle = _decided_cycle(cycle_id)
    decider = db.get_user(cycle.get("manager_id") or "") or {}
    row = next((r for r in insights.calibration()["queue"] if r["cycle_id"] == cycle["cycle_id"]), None)
    return render(request, "leader/calibration_detail.html", cycle=cycle, row=row, decider=decider,
                  reviews=api_client.get_calibration_reviews(cycle["cycle_id"]), error=None,
                  conversations=api_client.simulation_conversations(cycle))

@app.post("/leader/calibration/{cycle_id}/review")
def leader_calibration_review(request: Request, cycle_id: str, outcome: Annotated[str, Form()] = "", note: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role("leader", "admin"))):
    security.validate_csrf(request, csrf_token)
    cycle = _decided_cycle(cycle_id)
    outcome = security.clean_form_text(outcome, 20)
    note = security.clean_form_text(note, 2000)
    if outcome not in {"concur", "rereview"}:
        raise HTTPException(400, "Choose concur or re-review.")
    if outcome == "rereview" and len(note) < 10:
        row = next((r for r in insights.calibration()["queue"] if r["cycle_id"] == cycle["cycle_id"]), None)
        return render(request, "leader/calibration_detail.html", status_code=400, cycle=cycle, row=row,
                      decider=db.get_user(cycle.get("manager_id") or "") or {},
                      reviews=api_client.get_calibration_reviews(cycle["cycle_id"]),
                      conversations=api_client.simulation_conversations(cycle),
                      error="Explain what the manager should look at again (at least a sentence).")
    if not db.add_calibration_review(cycle["cycle_id"], user["id"], outcome, note):
        raise HTTPException(400, "This decision is already back with the manager for re-review.")
    if outcome == "rereview":
        # Undo what the decision set in motion: certified levels and booked training.
        progression.reopen_for_rereview(cycle["cycle_id"], date.today())
        performance.refresh([cycle["user_id"]])
    governance.record_audit(user["id"], user["role"], f"calibration_{outcome}", f"/leader/calibration/{cycle['cycle_id']}")
    request.session["flash"] = ("Re-review requested: the decision is back in the manager's validation queue."
                                if outcome == "rereview" else "Recorded: you concur with the manager's decision.")
    return RedirectResponse("/leader/calibration", status_code=303)


# ---------------- SME ----------------
@app.get("/sme/assessment-reviews")
def sme_assessment_reviews(request: Request, user: dict = Depends(auth.require_role("sme", "admin"))):
    return render(request, "sme/assessment_reviews.html", escalations=api_client.get_a3_escalations(), scored_cycles=api_client.get_sme_scored_cycles())

@app.post("/sme/assessment-reviews/{escalation_id}/decision")
def sme_assessment_review_decision(request: Request, escalation_id: str, decision: Annotated[str, Form()] = "accept", note: Annotated[str | None, Form()] = None, csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role("sme", "admin"))):
    security.validate_csrf(request, csrf_token)
    if not api_client.decide_a3_escalation(security.clean_form_text(escalation_id, 80), security.clean_form_text(decision, 30), security.clean_form_text(note or "", 2000)):
        raise HTTPException(400, "A3 review action could not be completed.")
    return RedirectResponse("/sme/assessment-reviews", status_code=303)

@app.get("/sme/dashboard")
def sme_dashboard(request: Request, user: dict = Depends(auth.require_role("sme", "admin"))): return render(request, "sme/dashboard.html", dashboard=api_client.get_sme_overview())
@app.get("/sme/approved")
def sme_approved(request: Request, user: dict = Depends(auth.require_role("sme", "admin"))): return render(request, "sme/approved.html", questions=api_client.get_sme_approved_questions())
def _level(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() and 1 <= int(value) <= 5 else None


def sme_generate_page(request: Request, status_code: int = 200, error: str | None = None, result: dict | None = None, role: str = ""):
    roles = db.list_roles()
    role = role if role in {r["role_id"] for r in roles} else (roles[0]["role_id"] if roles else "")
    return render(request, "sme/generate.html", status_code=status_code, roles=roles, selected_role=role,
                  role_skills=db.role_skills(role) if role else [], error=error, result=result)


@app.get("/sme/generate")
def sme_generate(request: Request, role: str = "", user: dict = Depends(auth.require_role("sme", "admin"))):
    return sme_generate_page(request, role=security.clean_form_text(role, 120))


@app.post("/sme/generate")
async def sme_generate_submit(request: Request, user: dict = Depends(auth.require_role("sme", "admin"))):
    form = await request.form()
    security.validate_csrf(request, form.get("csrf_token", ""))
    role_id = security.clean_form_text(form.get("role_id", ""), 120)
    if role_id not in {r["role_id"] for r in db.list_roles()}:
        return sme_generate_page(request, status_code=400, error="Choose a role.")
    try:
        count = max(1, min(10, int(form.get("count", "5"))))
    except ValueError:
        count = 5
    known = {s["skill"] for s in db.role_skills(role_id)}
    skills = [s for s in (security.clean_form_text(v, 200) for v in form.getlist("skills")) if s in known]
    if not api_client.backend_pipeline_enabled():
        return sme_generate_page(request, status_code=400, error="Backend API mode is not enabled.", role=role_id)
    try:
        result = api_client.generate_bank_questions(role_id, count, _level(form.get("level")), skills or None, user["id"])
    except BackendAPIError as exc:
        return sme_generate_page(request, status_code=503, error=str(exc), role=role_id)
    governance.record_audit(user["id"], user["role"], "bank_questions_generated", "/sme/generate",
                            details={"role_id": role_id, "count": len(result["items"])})
    return sme_generate_page(request, result=result, role=role_id)


@app.get("/sme/bank")
def sme_bank(request: Request, role: str = "", status: str = "approved", user: dict = Depends(auth.require_role("sme", "admin"))):
    role = security.clean_form_text(role, 120)
    status = status if status in {"approved", "pending", "rejected"} else "approved"
    questions = db.list_bank(role or None, status)
    counts: dict[str, int] = {}
    for q in db.list_bank(status="approved"):
        counts[q["role_id"]] = counts.get(q["role_id"], 0) + 1
    return render(request, "sme/bank.html", questions=questions, roles=db.list_roles(), role_counts=counts,
                  selected_role=role, selected_status=status)


@app.get("/sme/assign")
def sme_assign(request: Request, role: str = "", user: dict = Depends(auth.require_role("sme", "admin"))):
    return render(request, "sme/assign.html", roster=question_bank.roster(), roles=db.list_roles(),
                  selected_role=security.clean_form_text(role, 120), notice=request.session.pop("flash", None))


@app.post("/sme/assign/auto")
def sme_assign_auto(request: Request, csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role("sme", "admin"))):
    security.validate_csrf(request, csrf_token)
    result = question_bank.run_auto_baseline()
    governance.record_audit(user["id"], user["role"], "baseline_auto_assigned", "/sme/assign/auto", details=result)
    blocked = sum(result["blocked"].values())
    request.session["flash"] = (f"Auto-assigned an assessment to {result['assigned']} employee(s) from SME-approved questions."
                                + (f" {blocked} still need approved questions for their role in the bank." if blocked else ""))
    return RedirectResponse("/sme/assign", status_code=303)


def builder_page(request: Request, role_id: str, count: int, level: int | None, cycle_id: str = "",
                 employee_ids: list[str] = (), status_code: int = 200, error: str | None = None):
    roles = {r["role_id"]: r for r in db.list_roles()}
    cycle = db.get_cycle(cycle_id) if cycle_id else None
    if cycle:
        if cycle["status"] != "pending_sme_setup":
            raise HTTPException(404, "Submission not waiting for set-up")
        role_id = role_id if role_id in roles else cycle["matched_role_id"]
        cycle["employee_name"] = (db.get_user(cycle["user_id"]) or {}).get("full_name", cycle["user_id"])
    if role_id not in roles:
        raise HTTPException(404, "Unknown role")
    focus = []
    for uid in employee_ids:
        plan = tni.employee_tni(uid)
        focus += [s["skill"] for s in (plan or {}).get("skills", []) if not s["assessed"] and s["skill"] not in focus]
    selection = question_bank.select_questions(role_id, count, level, focus)
    roster_rows = [r for r in question_bank.roster()["rows"] if r["role_id"] == role_id]
    return render(request, "sme/builder.html", status_code=status_code, role=roles[role_id], roles=list(roles.values()),
                  count=count, level=level, cycle=cycle, selection=selection, employees=roster_rows,
                  preselected=set(employee_ids), today=date.today().isoformat(), error=error)


@app.get("/sme/assessments/new")
def sme_builder(request: Request, role: str = "", cycle: str = "", count: int = 8, level: str = "", employee: list[str] = Query(default=[]),
                user: dict = Depends(auth.require_role("sme", "admin"))):
    return builder_page(request, security.clean_form_text(role, 120), max(1, min(20, count)), _level(level),
                        security.clean_form_text(cycle, 40), [security.clean_form_text(e, 80) for e in employee][:50])


@app.post("/sme/assessments/deploy")
async def sme_deploy(request: Request, user: dict = Depends(auth.require_role("sme", "admin"))):
    form = await request.form()
    security.validate_csrf(request, form.get("csrf_token", ""))
    role_id = security.clean_form_text(form.get("role_id", ""), 120)
    cycle_id = security.clean_form_text(form.get("cycle_id", ""), 40)
    question_ids = [security.clean_form_text(q, 80) for q in form.getlist("question_ids")][:40]
    employee_ids = [security.clean_form_text(e, 80) for e in form.getlist("employee_ids")][:100]
    try:
        due = schedule.parse_date(form.get("due_date")) if form.get("due_date") else None
        if due and due < date.today():
            raise ValueError("Choose today or a future due date.")
        if not cycle_id and not employee_ids:
            raise ValueError("Choose at least one employee.")
        result = question_bank.deploy(role_id=role_id, question_ids=question_ids, deployed_by=user["id"],
                                      employee_ids=employee_ids, cycle_id=cycle_id or None, due_date=due)
    except ValueError as exc:
        try:
            count = max(1, min(20, int(form.get("count", "8"))))
        except ValueError:
            count = 8
        return builder_page(request, role_id, count, _level(form.get("level")), cycle_id, employee_ids, status_code=400, error=str(exc))
    governance.record_audit(user["id"], user["role"], "assessment_deployed", "/sme/assessments/deploy",
                            details={"role_id": role_id, "questions": len(question_ids), "assigned": len(result["assigned"])})
    skipped = "; ".join(f"{uid}: {why}" for uid, why in result["skipped"])
    request.session["flash"] = (f"Assessment deployed to {len(result['assigned'])} employee(s) with {len(set(question_ids))} question(s)."
                                + (f" Skipped — {skipped}." if skipped else ""))
    return RedirectResponse("/sme/assign", status_code=303)


@app.get("/sme/questions")
def sme_questions(request: Request, user: dict = Depends(auth.require_role("sme", "admin"))):
    return render(request, "sme/questions.html", questions=api_client.get_sme_pending_queue())

@app.get("/sme/questions/{question_id}")
def sme_question_review(request: Request, question_id: str, user: dict = Depends(auth.require_role("sme", "admin"))):
    question = api_client.get_sme_question_detail(question_id)
    if not question: raise HTTPException(404, "Question not found")
    return render(request, "sme/review.html", question=question)

@app.post("/sme/questions/{question_id}/approve")
def approve_question(request: Request, question_id: str, csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role("sme", "admin"))):
    security.validate_csrf(request, csrf_token)
    if not api_client.decide_sme_question(security.clean_form_text(question_id, 80), "approved", user["id"]): raise HTTPException(404, "Question not found")
    return RedirectResponse("/sme/questions", status_code=303)

@app.post("/sme/questions/{question_id}/reject")
def reject_question(request: Request, question_id: str, csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role("sme", "admin"))):
    security.validate_csrf(request, csrf_token)
    if not api_client.decide_sme_question(security.clean_form_text(question_id, 80), "rejected", user["id"]): raise HTTPException(404, "Question not found")
    return RedirectResponse("/sme/questions", status_code=303)


# ---------------- Admin / Governance ----------------
def admin_page(request: Request, template: str, title: str):
    return render(request, template, page_title=title, dashboard=api_client.get_admin_dashboard(), users=api_client.get_users(), roles=auth.USER_ROLES, skills=api_client.get_role_skills(), questions=api_client.get_questions())

@app.get("/admin/dashboard")
def admin_dashboard(request: Request, user: dict = Depends(auth.require_role("admin"))): return admin_page(request, "admin/dashboard.html", "Administration")
@app.get("/admin/users")
def admin_users(request: Request, user: dict = Depends(auth.require_role("admin"))):
    return render(request, "admin/users.html", page_title="User Management", users=api_client.get_all_users(), managers=api_client.get_manager_options(), roles=auth.USER_ROLES,
                  notice=request.session.pop("flash", None))

@app.post("/admin/users")
def admin_create_user(
    request: Request,
    username: Annotated[str, Form()], email: Annotated[str, Form()], password: Annotated[str, Form()],
    full_name: Annotated[str, Form()], role: Annotated[str, Form()],
    department: Annotated[str, Form()] = "", job_title: Annotated[str, Form()] = "",
    manager_id: Annotated[str | None, Form()] = None,
    csrf_token: Annotated[str, Form()] = "",
    user: dict = Depends(auth.require_role("admin")),
):
    security.validate_csrf(request, csrf_token)
    if role not in auth.USER_ROLES:
        raise HTTPException(400, "Unknown role")
    try:
        clean_username = security.validate_username(security.clean_form_text(username, 60))
        clean_email = security.validate_email(security.clean_form_text(email, 120))
        security.validate_password(password, clean_username)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if manager_id and manager_id not in {m["user_id"] for m in api_client.get_manager_options()}:
        raise HTTPException(400, "Unknown manager")
    try:
        created = api_client.create_portal_user(
            username=clean_username, email=clean_email,
            password=password, full_name=security.clean_form_text(full_name, 120), role=role,
            department=security.clean_form_text(department, 120), job_title=security.clean_form_text(job_title, 120),
            manager_id=manager_id or None,
        )
    except sqlite3.IntegrityError:
        raise HTTPException(400, "A user with that username or email already exists.")
    governance.record_audit(user["id"], user["role"], "user_created", "/admin/users", details={"new_user_id": created["user_id"], "role": role})
    if role == "employee":
        performance.refresh([created["user_id"]], escalate=False)  # shows up under their manager straight away
    return RedirectResponse("/admin/users", status_code=303)

@app.post("/admin/users/{user_id}/role")
def admin_update_user_role(request: Request, user_id: str, role: Annotated[str, Form()], manager_id: Annotated[str | None, Form()] = None, csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role("admin"))):
    security.validate_csrf(request, csrf_token)
    if role not in auth.USER_ROLES:
        raise HTTPException(400, "Unknown role")
    target_id = security.clean_form_text(user_id, 80)
    if target_id == user["id"] and role != "admin":
        raise HTTPException(400, "You cannot remove your own administrator role.")
    if manager_id and manager_id not in {m["user_id"] for m in api_client.get_manager_options()}:
        raise HTTPException(400, "Unknown manager")
    api_client.update_portal_user(target_id, role, manager_id or None)
    governance.record_audit(user["id"], user["role"], "user_role_changed", f"/admin/users/{target_id}/role",
                            details={"new_role": role, "manager_id": manager_id or None})
    if role == "employee":
        performance.refresh([target_id], escalate=False)  # the leader's manager clusters follow the new line manager
    else:
        lifecycle_store.drop_status(target_id)
    request.session["flash"] = f"Updated {target_id}: {auth.USER_ROLES[role]}" + (" (HR access granted)." if role == "hr" else ".")
    return RedirectResponse("/admin/users", status_code=303)
@app.get("/admin/roles")
def admin_roles(request: Request, user: dict = Depends(auth.require_role("admin"))): return admin_page(request, "admin/roles.html", "Role Management")
@app.get("/admin/skills")
def admin_skills(request: Request, user: dict = Depends(auth.require_role("admin"))):
    # The live skill catalogue (add skills, Agent 8 recommendations) is shared with HR partners.
    return RedirectResponse("/org/skills", status_code=303)
@app.get("/admin/assessments")
def admin_assessments(request: Request, user: dict = Depends(auth.require_role("admin"))): return admin_page(request, "admin/assessments.html", "Assessment Configuration")
@app.get("/admin/questions")
def admin_questions(request: Request, user: dict = Depends(auth.require_role("admin"))): return admin_page(request, "admin/questions.html", "Question Bank")
@app.get("/admin/reports")
def admin_reports(request: Request, user: dict = Depends(auth.require_role("admin"))): return admin_page(request, "admin/reports.html", "Platform Reports")
@app.get("/admin/settings")
def admin_settings(request: Request, user: dict = Depends(auth.require_role("admin"))): return admin_page(request, "admin/settings.html", "System Settings")

@app.get("/admin/audit")
def admin_audit(request: Request, user: dict = Depends(auth.require_role("admin"))):
    return render(request, "admin/audit.html", events=governance.get_audit_events(), runs=governance.get_agent_runs(), calls=governance.get_llm_calls())

@app.get("/admin/budget")
def admin_budget(request: Request, user: dict = Depends(auth.require_role("admin"))):
    return render(request, "admin/budget.html", budget=governance.get_budget_snapshot(), daily_cap=governance.DAILY_TOKEN_CAP, agent_caps=governance.AGENT_CAPS)
