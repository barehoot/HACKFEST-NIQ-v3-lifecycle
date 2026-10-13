"""The employee's own lifecycle page: their status and what drives it, the
evidence shared with them (with a right of reply), reviews to acknowledge,
their development actions, probation timeline, a weekly pulse check-in and
peer recognition -- plus the coding task inside the workplace simulation.

Transparency rule: an employee sees the same status factors their manager
sees, and only evidence shared with them counts towards that status.
"""
from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from domain import governance
from services import (api_client, auth, coding, db, development, engagement, evidence, lifecycle_store as store,
                      performance, probation, security)
from routes.web import employee_scope, flash, pop_flash, render

router = APIRouter()
EMPLOYEE = ("employee", "admin")


def _back(anchor: str) -> RedirectResponse:
    return RedirectResponse(f"/employee/performance#{anchor}", status_code=303)


def performance_page(request: Request, user: dict, status_code: int = 200, error: str | None = None):
    uid = employee_scope(user)
    today = date.today()
    status = performance.status_for(uid, today)
    manager_id = (db.get_user(uid) or {}).get("manager_id")
    colleagues = [u for u in store.users_by_ids(store.employee_ids(manager_id)) if u["user_id"] != uid] if manager_id else []
    return render(request, "employee/performance.html", status_code=status_code, status=status,
                  evidence=evidence.timeline(uid, employee_view=True), reviews=store.list_reviews(uid, include_drafts=False),
                  plan=development.view(uid, today), probation=probation.view(uid, today),
                  pulse=engagement.my_pulse(uid, today), colleagues=colleagues, role_skills=evidence.role_skills(uid),
                  error=error, notice=pop_flash(request))


@router.get("/employee/performance")
def my_performance(request: Request, user: dict = Depends(auth.require_role(*EMPLOYEE))):
    return performance_page(request, user)


@router.post("/employee/evidence/{evidence_id}/comment")
def my_evidence_comment(request: Request, evidence_id: str, comment: Annotated[str, Form()] = "",
                        csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role(*EMPLOYEE))):
    security.validate_csrf(request, csrf_token)
    text = security.clean_multiline_text(comment, 1500)
    if len(text) < 3:
        return performance_page(request, user, status_code=400, error="Write your comment before saving it.")
    if not store.set_evidence_comment(security.clean_form_text(evidence_id, 40), employee_scope(user), text):
        raise HTTPException(404, "Evidence not found")
    manager = engagement.manager_of(employee_scope(user))
    engagement.notify([manager], "evidence_comment", f"{user['name']} commented on an evidence item", text[:200],
                      f"/people/{employee_scope(user)}#evidence")
    return _back("evidence")


@router.post("/employee/reviews/{review_id}/acknowledge")
def my_review_acknowledge(request: Request, review_id: str, comment: Annotated[str, Form()] = "",
                          csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role(*EMPLOYEE))):
    security.validate_csrf(request, csrf_token)
    if not store.acknowledge_review(security.clean_form_text(review_id, 40), employee_scope(user),
                                    security.clean_multiline_text(comment, 2000)):
        raise HTTPException(404, "Review not found")
    governance.record_audit(user["id"], user["role"], "review_acknowledged", "/employee/performance")
    engagement.notify([engagement.manager_of(employee_scope(user))], "review", f"{user['name']} acknowledged their review",
                      security.clean_form_text(comment, 200) or "No comment added.", f"/people/{employee_scope(user)}#reviews")
    flash(request, "Review acknowledged.")
    return _back("reviews")


@router.post("/employee/actions/{action_id}/status")
def my_action_status(request: Request, action_id: str, status: Annotated[str, Form()] = "",
                     csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role(*EMPLOYEE))):
    security.validate_csrf(request, csrf_token)
    action = store.get_action(security.clean_form_text(action_id, 40))
    # The employee moves their own actions forward; cancelling is the manager's call.
    if not action or action["user_id"] != employee_scope(user) or status not in ("open", "in_progress", "done"):
        raise HTTPException(404, "Action not found")
    store.set_action_status(action["action_id"], status)
    performance.refresh([employee_scope(user)])
    if status == "done":
        engagement.notify([engagement.manager_of(employee_scope(user))], "development",
                          f"{user['name']} completed a development action", action["title"],
                          f"/people/{employee_scope(user)}#development")
    return _back("development")


@router.post("/employee/pulse")
def my_pulse(request: Request, mood: Annotated[int, Form()] = 0, workload: Annotated[int, Form()] = 0,
             comment: Annotated[str, Form()] = "", shared: Annotated[str, Form()] = "",
             csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role(*EMPLOYEE))):
    security.validate_csrf(request, csrf_token)
    try:
        engagement.record_pulse(employee_scope(user), mood, workload, comment, bool(shared), date.today())
    except ValueError as exc:
        return performance_page(request, user, status_code=400, error=str(exc))
    performance.refresh([employee_scope(user)], escalate=False)
    flash(request, "Thanks -- your check-in is saved for this week.")
    return _back("pulse")


@router.post("/employee/kudos")
def my_kudos(request: Request, recipient_id: Annotated[str, Form()] = "", message: Annotated[str, Form()] = "",
             skill: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "",
             user: dict = Depends(auth.require_role(*EMPLOYEE))):
    security.validate_csrf(request, csrf_token)
    sender = {**user, "id": employee_scope(user)}
    try:
        engagement.send_kudos(sender=sender, recipient_id=security.clean_form_text(recipient_id, 80), message=message,
                              skill=security.clean_form_text(skill, 120) or None, today=date.today())
    except ValueError as exc:
        return performance_page(request, user, status_code=400, error=str(exc))
    governance.record_audit(user["id"], user["role"], "kudos_sent", "/employee/kudos")
    flash(request, "Recognition sent. Their manager will add it to their record.")
    return _back("kudos")


# ---------------------------------------------------------------------------
# Coding task inside the workplace simulation
# ---------------------------------------------------------------------------

@router.post("/employee/evidence/{cycle_id}/code")
def my_code_submit(request: Request, cycle_id: str, challenge_id: Annotated[str, Form()] = "",
                   code: Annotated[str, Form()] = "", paste_chars: Annotated[str, Form()] = "",
                   csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role(*EMPLOYEE))):
    security.validate_csrf(request, csrf_token)
    cycle = api_client.get_employee_cycle(employee_scope(user))
    if not cycle or cycle["cycle_id"] != cycle_id or cycle["status"] != "mcq_submitted":
        raise HTTPException(404, "Simulation not available")
    pasted = min(int(paste_chars), 100_000) if paste_chars.strip().isdigit() else None  # integrity telemetry only
    try:
        result = coding.submit(cycle, employee_scope(user), security.clean_form_text(challenge_id, 40), code, pasted)
    except coding.CodingError as exc:
        flash(request, f"Coding task not submitted: {exc}")
        return RedirectResponse(f"/employee/evidence/{cycle_id}#coding", status_code=303)
    governance.record_audit(user["id"], user["role"], "coding_submitted", f"/employee/evidence/{cycle_id}",
                            details={"challenge_id": challenge_id, "ai_reviewed": result["ai_reviewed"]})
    flash(request, "Coding task submitted. It is reviewed, never run, and an SME confirms the score.")
    return RedirectResponse(f"/employee/evidence/{cycle_id}#coding", status_code=303)
