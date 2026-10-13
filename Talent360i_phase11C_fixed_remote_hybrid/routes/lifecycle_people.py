"""People views for managers, business leaders and admins: the employee-card
directory, the quick-view card, the 360 profile, and everything a manager
records about a report -- evidence, reviews, development plan, probation --
plus the leader's performance overview and probation decisions.

Access: a manager sees and acts on their direct reports only; a leader sees
their departments (everyone when they lead none yet) and decides probation and
leader-level escalations; an HR partner reads everyone (and records nothing about
performance); an admin sees everyone. Every denied lookup is a 404 and is audited
(routes/web.person_in_scope).

Leaders, HR and admins open the directory on manager clusters: one tile per line
manager with the team's band mix and signals; a click shows that manager's reports.
Searching for a person always lists people.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse

from domain import governance
from services import (api_client, auth, development, engagement, evidence, job_profiles, lifecycle_store as store, mobility,
                      performance, performance_summary, probation, progression, security)
from services.backend_api import BackendAPIError
from routes.hr import team_flags
from routes.web import flash, leader_scope, manager_scope, person_in_scope, pop_flash, render

router = APIRouter()
MANAGERS = ("manager", "admin")
VIEWERS = ("manager", "leader", "admin", "hr")
LEADERS = ("leader", "admin")
ORG_VIEWERS = ("leader", "admin", "hr")  # see manager clusters first
PAGE_SIZE = 24
SORTS = {"attention": "Needs attention first", "name": "Name", "score_desc": "Highest score", "score_asc": "Lowest score"}
CLUSTER_SORTS = {"attention": "Needs attention first", "name": "Manager name", "team_desc": "Largest team",
                 "score_asc": "Lowest average score", "score_desc": "Highest average score"}
NO_MANAGER = "none"
SIGNAL_FILTERS = {"performance_concern": "Performance concern", "promotion_ready": "Promotion ready",
                  "probation_decision": "Probation action", "probation": "On probation", "escalation": "Escalated",
                  "one_on_one_overdue": "1:1 overdue", "wellbeing": "Wellbeing check-in"}


def _scope_filters(user: dict) -> dict:
    if user["role"] == "manager":
        return {"manager_id": user["id"]}
    if user["role"] == "leader":
        depts = leader_scope(user)
        return {"dept_ids": depts} if depts is not None else {}
    return {}


def _scope_ids(user: dict) -> set[str] | None:
    """Employee ids a leader is responsible for, or None for the whole organisation."""
    scope = _scope_filters(user)
    return None if not scope else store.status_ids(scope)


def _back(employee_id: str, anchor: str = "") -> RedirectResponse:
    return RedirectResponse(f"/people/{employee_id}{'#' + anchor if anchor else ''}", status_code=303)


def _not_read_only(user: dict) -> None:
    """HR partners read people records org-wide but act on none of them."""
    if user["role"] == "hr":
        raise HTTPException(403, "Access restricted")


def _manager_of(user: dict, employee_id: str, action: str) -> dict:
    """Write access: the employee's line manager (or an admin)."""
    if user["role"] not in MANAGERS:
        raise HTTPException(403, "Access restricted")
    return person_in_scope(user, employee_id, action)


# ---------------------------------------------------------------------------
# Directory and cards
# ---------------------------------------------------------------------------

@router.get("/people")
def people_directory(request: Request, q: str = "", band: str = "", signal: str = "", dept: str = "", tower: str = "",
                     sort: str = "attention", page: int = 1, manager: str = "", view: str = "",
                     user: dict = Depends(auth.require_role(*VIEWERS))):
    today = date.today()
    if user["role"] == "manager":
        performance.team_cards(manager_scope(user), today)  # keeps the team's snapshot fresh
    else:
        performance.refresh_missing(today)  # org views read the snapshot the worker keeps fresh
    signal_options = {k: v for k, v in SIGNAL_FILTERS.items()
                      if k not in performance.MANAGER_ONLY_SIGNALS or user["role"] == "manager"}
    filters = {**_scope_filters(user), "q": security.clean_form_text(q, 80),
               "band": band if band in performance.BANDS else "", "signal": signal if signal in signal_options else "",
               "dept_id": security.clean_form_text(dept, 40), "tower": tower if tower in job_profiles.towers() else ""}
    scope = _scope_filters(user)
    page = max(1, min(page, 10_000))
    manager = security.clean_form_text(manager, 80) if user["role"] in ORG_VIEWERS else ""
    view = "people" if view == "people" else ""
    context = dict(filters=filters, signals=signal_options, bands=performance.BANDS, tiers=performance.TIERS,
                   departments=store.list_departments(), towers=job_profiles.towers(), notice=pop_flash(request),
                   manager=manager, view=view, org_viewer=user["role"] in ORG_VIEWERS)
    if user["role"] in ORG_VIEWERS and not manager and not filters["q"] and view != "people":
        sort = sort if sort in CLUSTER_SORTS else "attention"
        rows, total = store.manager_clusters(filters, page=page, page_size=PAGE_SIZE, sort=sort)
        for row in rows:
            row["flags"] = team_flags(row)
        pages = max(1, -(-total // PAGE_SIZE))
        return render(request, "people/directory.html", mode="clusters", clusters=rows, sort=sort, sorts=CLUSTER_SORTS,
                      result={"total": total, "page": min(page, pages), "pages": pages, "page_size": PAGE_SIZE},
                      summary=performance.band_strip(scope), **context)
    lead = None
    if manager:
        filters.update({"no_manager": True} if manager == NO_MANAGER else {"manager_id": manager})
        clusters, _ = store.manager_clusters({**scope, **({"no_manager": True} if manager == NO_MANAGER else {"manager_id": manager})},
                                             page_size=1)
        if not clusters:
            governance.record_audit(user["id"], user["role"], "people_cluster", f"/people?manager={manager}", outcome="denied")
            raise HTTPException(404, "Manager not found")
        lead = {**clusters[0], "flags": team_flags(clusters[0])}
    sort = sort if sort in SORTS else "attention"
    result = performance.directory(filters, page, sort, PAGE_SIZE, viewer_role=user["role"])
    band_scope = {**scope, **({"no_manager": True} if manager == NO_MANAGER else {"manager_id": manager} if manager else {})}
    return render(request, "people/directory.html", mode="people", result=result, sort=sort, sorts=SORTS, lead=lead,
                  summary=performance.band_strip(band_scope), **context)


@router.post("/people/refresh")
def people_refresh(request: Request, csrf_token: Annotated[str, Form()] = "",
                   user: dict = Depends(auth.require_role(*ORG_VIEWERS))):
    security.validate_csrf(request, csrf_token)
    count = performance.refresh_stale(date.today(), minutes=0, limit=20_000)
    governance.record_audit(user["id"], user["role"], "status_refresh", "/people/refresh", details={"people": count})
    flash(request, f"Recalculated the status of {count} employee(s).")
    return RedirectResponse("/people", status_code=303)


@router.get("/people/{employee_id}/card")
def people_quick_card(request: Request, employee_id: str, user: dict = Depends(auth.require_role(*VIEWERS))):
    employee = person_in_scope(user, employee_id, "people_card")
    if request.headers.get("x-requested-with") != "fetch":
        return RedirectResponse(f"/people/{employee['user_id']}", status_code=303)  # a fragment is no page on its own
    today = date.today()
    status = performance.for_viewer(performance.status_for(employee["user_id"], today), user["role"])
    items = evidence.timeline(employee["user_id"], employee_view=False)
    return render(request, "people/quick.html", employee=employee, status=status,
                  card=performance.card(store.status_rows([employee["user_id"]])[employee["user_id"]], user["role"]),
                  evidence=items[:3], counts=evidence.counts(items, today),
                  plan=development.view(employee["user_id"], today), probation=probation.view(employee["user_id"], today),
                  review=store.latest_reviews([employee["user_id"]]).get(employee["user_id"]),
                  can_edit=user["role"] in MANAGERS)


# ---------------------------------------------------------------------------
# 360 profile
# ---------------------------------------------------------------------------

def _current_period(today: date) -> str:
    return f"{today.year}-H{1 if today.month <= 6 else 2}"


def profile_page(request: Request, user: dict, employee_id: str, status_code: int = 200, error: str | None = None,
                 review_draft: dict | None = None, review_period: str | None = None):
    employee = person_in_scope(user, employee_id, "people_profile")
    uid = employee["user_id"]
    today = date.today()
    status = performance.for_viewer(performance.status_for(uid, today), user["role"])
    ctx = status["context"]
    items = evidence.timeline(uid, employee_view=False)
    reviews = store.list_reviews(uid)
    period = review_period or _current_period(today)
    return render(
        request, "people/profile.html", status_code=status_code, employee=employee, status=status,
        card=performance.card(store.status_rows([uid])[uid], user["role"]), evidence=items, pending=evidence.pending(uid),
        counts=evidence.counts(items, today), kinds=evidence.KIND_LABELS, role_skills=evidence.role_skills(uid),
        reviews=reviews, period=period, period_review=next((r for r in reviews if r["period"] == period), None),
        review_draft=review_draft, plan=development.view(uid, today), action_types=development.ACTION_LABELS,
        probation=probation.view(uid, today), ratings=probation.RATINGS, recommendations=probation.RECOMMENDATIONS,
        escalations=store.list_escalations(user_ids=[uid], statuses=("open", "acknowledged", "resolved")),
        insight=store.latest_insight(uid), tni=ctx["plan"], readiness=ctx["readiness"],
        cycles=progression._history(uid), profile=store.get_profile(uid), today=today.isoformat(),
        can_edit=user["role"] in MANAGERS, is_leader=user["role"] in LEADERS, read_only=user["role"] == "hr",
        summaries=store.latest_summaries(uid), summary_types=performance_summary.TYPES,
        promotion_targets=performance_summary.promotion_targets(uid), aspirations=mobility.aspirations(uid),
        applications=store.list_applications(user_id=uid), cv_claims=store.cv_claims([uid]).get(uid, []),
        error=error, notice=pop_flash(request))


@router.get("/people/{employee_id}")
def people_profile(request: Request, employee_id: str, user: dict = Depends(auth.require_role(*VIEWERS))):
    return profile_page(request, user, employee_id)


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

@router.post("/people/{employee_id}/evidence")
def evidence_add(request: Request, employee_id: str, kind: Annotated[str, Form()] = "", title: Annotated[str, Form()] = "",
                 summary: Annotated[str, Form()] = "", occurred_on: Annotated[str, Form()] = "",
                 skills: Annotated[list[str], Form()] = [], weight: Annotated[int, Form()] = 1,  # noqa: B006
                 shared: Annotated[str, Form()] = "", action_items: Annotated[str, Form()] = "",
                 create_actions: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "",
                 user: dict = Depends(auth.require_role(*VIEWERS))):
    security.validate_csrf(request, csrf_token)
    employee = _manager_of(user, employee_id, "evidence_add")
    try:
        evidence.add_manual(employee["user_id"], kind=kind, title=title, summary=summary, occurred_on=occurred_on,
                            skills=[security.clean_form_text(s, 120) for s in skills[:6]], weight=max(1, min(3, weight)),
                            shared=bool(shared), action_items=action_items.splitlines(), create_actions=bool(create_actions),
                            by=user["id"], today=date.today())
    except evidence.EvidenceError as exc:
        return profile_page(request, user, employee_id, status_code=400, error=str(exc))
    governance.record_audit(user["id"], user["role"], "evidence_added", f"/people/{employee['user_id']}", details={"kind": kind})
    performance.refresh([employee["user_id"]])
    flash(request, f"{evidence.KIND_LABELS.get(kind, 'Evidence')} recorded for {employee['full_name']}.")
    return _back(employee["user_id"], "evidence")


@router.post("/people/{employee_id}/evidence/import")
async def evidence_import(request: Request, employee_id: str, source: Annotated[str, Form()] = "teams",
                          pasted: Annotated[str, Form()] = "", message_file: Annotated[UploadFile | None, File()] = None,
                          csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role(*VIEWERS))):
    security.validate_csrf(request, csrf_token)
    employee = _manager_of(user, employee_id, "evidence_import")
    today = date.today()
    try:
        if message_file is not None and message_file.filename:
            data = await message_file.read(evidence.MAX_IMPORT_BYTES + 1)
            parsed = evidence.parse_upload(message_file.filename, data)
        else:
            parsed = {"subject": "", "sender": None, "sent_on": None, "body": pasted,
                      "source": source if source in ("outlook", "teams") else "teams"}
        evidence.import_message(employee["user_id"], source=parsed["source"], subject=parsed["subject"], body=parsed["body"],
                                sender=parsed["sender"], sent_on=parsed["sent_on"], by=user["id"], today=today)
    except evidence.EvidenceError as exc:
        return profile_page(request, user, employee_id, status_code=400, error=str(exc))
    governance.record_audit(user["id"], user["role"], "evidence_imported", f"/people/{employee['user_id']}",
                            details={"source": parsed["source"]})
    flash(request, "Message tagged. Agent 7 suggested how to record it -- review and confirm it below.")
    return _back(employee["user_id"], "pending")


@router.post("/people/{employee_id}/evidence/{evidence_id}/confirm")
def evidence_confirm(request: Request, employee_id: str, evidence_id: str, kind: Annotated[str, Form()] = "",
                     sentiment: Annotated[str, Form()] = "", title: Annotated[str, Form()] = "",
                     summary: Annotated[str, Form()] = "", skills: Annotated[list[str], Form()] = [],  # noqa: B006
                     shared: Annotated[str, Form()] = "", weight: Annotated[int, Form()] = 1,
                     create_actions: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "",
                     user: dict = Depends(auth.require_role(*VIEWERS))):
    security.validate_csrf(request, csrf_token)
    employee = _manager_of(user, employee_id, "evidence_confirm")
    item = store.get_evidence(security.clean_form_text(evidence_id, 40))
    if not item or item["user_id"] != employee["user_id"] or item["status"] != "pending":
        raise HTTPException(404, "Suggestion not found")
    try:
        evidence.confirm(item, user["id"], kind=kind, sentiment=sentiment, title=title, summary=summary,
                         skills=[security.clean_form_text(s, 120) for s in skills[:6]], shared=bool(shared),
                         weight=weight, create_actions=bool(create_actions), today=date.today())
    except evidence.EvidenceError as exc:
        return profile_page(request, user, employee_id, status_code=400, error=str(exc))
    governance.record_audit(user["id"], user["role"], "evidence_confirmed", f"/people/{employee['user_id']}",
                            details={"evidence_id": item["evidence_id"], "kind": kind,
                                     "changed_ai_kind": kind != (item["ai"] or {}).get("kind")})
    performance.refresh([employee["user_id"]])
    flash(request, "Evidence confirmed and added to the record.")
    return _back(employee["user_id"], "evidence")


@router.post("/people/{employee_id}/evidence/{evidence_id}/{action}")
def evidence_withdraw(request: Request, employee_id: str, evidence_id: str, action: str,
                      csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role(*VIEWERS))):
    security.validate_csrf(request, csrf_token)
    if action not in ("dismiss", "retract"):
        raise HTTPException(404, "Not found")
    employee = _manager_of(user, employee_id, f"evidence_{action}")
    item = store.get_evidence(security.clean_form_text(evidence_id, 40))
    if not item or item["user_id"] != employee["user_id"]:
        raise HTTPException(404, "Evidence not found")
    ok = (store.decide_evidence(item["evidence_id"], "dismissed", user["id"]) if action == "dismiss"
          else store.retract_evidence(item["evidence_id"], user["id"]))
    if not ok:
        raise HTTPException(400, "That item can't be changed.")
    governance.record_audit(user["id"], user["role"], f"evidence_{action}", f"/people/{employee['user_id']}",
                            details={"evidence_id": item["evidence_id"]})
    performance.refresh([employee["user_id"]])
    flash(request, "Suggestion dismissed." if action == "dismiss" else "Evidence withdrawn from the record.")
    return _back(employee["user_id"], "evidence")


# ---------------------------------------------------------------------------
# AI insight and reviews
# ---------------------------------------------------------------------------

@router.post("/people/{employee_id}/insight")
def people_insight(request: Request, employee_id: str, csrf_token: Annotated[str, Form()] = "",
                   user: dict = Depends(auth.require_role(*VIEWERS))):
    security.validate_csrf(request, csrf_token)
    _not_read_only(user)
    employee = person_in_scope(user, employee_id, "insight_requested")
    status = performance.status_for(employee["user_id"])
    try:
        result = api_client.backend_performance_insight(performance.insight_payload(employee["user_id"], status), mode="insight")
    except BackendAPIError:
        return profile_page(request, user, employee_id, status_code=503,
                            error="The AI insight service is unavailable right now. The status and factors above are unaffected.")
    store.save_insight(employee["user_id"], result, result.get("generated_by") or "agent", user["id"])
    governance.record_audit(user["id"], user["role"], "insight_generated", f"/people/{employee['user_id']}",
                            details={"generated_by": result.get("generated_by")})
    return _back(employee["user_id"], "insight")


def _rating(value: str) -> int | None:
    return int(value) if value.isdigit() and 1 <= int(value) <= 5 else None


@router.post("/people/{employee_id}/reviews")
def review_save(request: Request, employee_id: str, period: Annotated[str, Form()] = "", rating: Annotated[str, Form()] = "",
                summary: Annotated[str, Form()] = "", strengths: Annotated[str, Form()] = "",
                improvements: Annotated[str, Form()] = "", share: Annotated[str, Form()] = "",
                ai_assisted: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "",
                user: dict = Depends(auth.require_role(*VIEWERS))):
    security.validate_csrf(request, csrf_token)
    employee = _manager_of(user, employee_id, "review_saved")
    period = security.clean_form_text(period, 10)
    summary = security.clean_multiline_text(summary, 3000)
    if not re.fullmatch(r"\d{4}-(H[12]|Q[1-4]|FY)", period):
        return profile_page(request, user, employee_id, status_code=400,
                            error="Choose a review period like 2026-H1 (mid-year), 2026-FY (annual) or 2026-Q3.")
    if share and (_rating(rating) is None or len(summary) < 40):
        return profile_page(request, user, employee_id, status_code=400,
                            error="A review you share needs a rating and a summary of at least a few sentences.")
    review_id = store.save_review(user_id=employee["user_id"], period=period, manager_id=user["id"], rating=_rating(rating),
                                  summary=summary, strengths=security.clean_multiline_text(strengths, 2000),
                                  improvements=security.clean_multiline_text(improvements, 2000), share=bool(share),
                                  ai_assisted=bool(ai_assisted))
    if not review_id:
        return profile_page(request, user, employee_id, status_code=400,
                            error="That review was already acknowledged by the employee and is locked.")
    governance.record_audit(user["id"], user["role"], "review_shared" if share else "review_drafted",
                            f"/people/{employee['user_id']}", details={"period": period, "rating": _rating(rating)})
    if share:
        engagement.notify([employee["user_id"]], "review", f"Your {period} performance review is ready",
                          "Read it and acknowledge it, adding your comments.", "/employee/performance#reviews")
    performance.refresh([employee["user_id"]])
    flash(request, f"Review {'shared with' if share else 'saved as a draft for'} {employee['full_name']}.")
    return _back(employee["user_id"], "reviews")


@router.post("/people/{employee_id}/reviews/draft")
def review_ai_draft(request: Request, employee_id: str, csrf_token: Annotated[str, Form()] = "",
                    user: dict = Depends(auth.require_role(*VIEWERS))):
    security.validate_csrf(request, csrf_token)
    employee = _manager_of(user, employee_id, "review_ai_draft")
    status = performance.status_for(employee["user_id"])
    try:
        result = api_client.backend_performance_insight(performance.insight_payload(employee["user_id"], status), mode="review")
    except BackendAPIError:
        return profile_page(request, user, employee_id, status_code=503,
                            error="The AI drafting service is unavailable right now. You can still write the review yourself.")
    governance.record_audit(user["id"], user["role"], "review_ai_draft", f"/people/{employee['user_id']}",
                            details={"generated_by": result.get("generated_by")})
    draft = {**(result.get("review_draft") or {}), "generated_by": result.get("generated_by")}
    return profile_page(request, user, employee_id, review_draft=draft)


# ---------------------------------------------------------------------------
# Performance summaries (Agent 9): mid-year, annual, 1:1, promotion readiness
# ---------------------------------------------------------------------------

@router.post("/people/{employee_id}/summaries")
def summary_generate(request: Request, employee_id: str, review_type: Annotated[str, Form()] = "",
                     target_role_id: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "",
                     user: dict = Depends(auth.require_role(*VIEWERS))):
    security.validate_csrf(request, csrf_token)
    _not_read_only(user)
    employee = person_in_scope(user, employee_id, "summary_requested")
    review_type = security.clean_form_text(review_type, 20)
    try:
        performance_summary.generate(employee["user_id"], review_type, user["id"], date.today(),
                                     security.clean_form_text(target_role_id, 120) or None)
    except performance_summary.SummaryError as exc:
        return profile_page(request, user, employee_id, status_code=400 if review_type not in performance_summary.TYPES else 503,
                            error=str(exc))
    governance.record_audit(user["id"], user["role"], "performance_summary", f"/people/{employee['user_id']}",
                            details={"review_type": review_type})
    flash(request, f"{performance_summary.TYPES[review_type]} summary ready: check it against the fact sheet beside it.")
    return RedirectResponse(f"/people/{employee['user_id']}?summary={review_type}#summary", status_code=303)


@router.post("/people/{employee_id}/summaries/{summary_id}/use")
def summary_use_in_review(request: Request, employee_id: str, summary_id: str, csrf_token: Annotated[str, Form()] = "",
                          user: dict = Depends(auth.require_role(*VIEWERS))):
    security.validate_csrf(request, csrf_token)
    employee = _manager_of(user, employee_id, "summary_use")
    summary = store.get_summary(security.clean_form_text(summary_id, 40))
    if not summary or summary["user_id"] != employee["user_id"] or summary["review_type"] not in ("mid_year", "annual"):
        raise HTTPException(404, "Summary not found")
    return profile_page(request, user, employee_id, review_draft=performance_summary.review_draft(summary),
                        review_period=performance_summary.review_period(summary["review_type"], date.today()))


# ---------------------------------------------------------------------------
# Career mobility on the profile: cross-department readiness and nominations
# ---------------------------------------------------------------------------

def _aspired_role(employee_id: str, role_id: str) -> str:
    role_id = security.clean_form_text(role_id, 120)
    targets = {t["role_id"] for t in performance_summary.promotion_targets(employee_id)}
    if role_id not in targets:
        raise HTTPException(404, "Not one of this employee's target roles")
    return role_id


@router.post("/people/{employee_id}/career/assess")
def career_assess(request: Request, employee_id: str, role_id: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "",
                  user: dict = Depends(auth.require_role(*VIEWERS))):
    security.validate_csrf(request, csrf_token)
    employee = _manager_of(user, employee_id, "career_assessment")
    role_id = _aspired_role(employee["user_id"], role_id)
    try:
        cycle_id = progression.request_promotion_assessment(employee["user_id"], user["id"], role_id)
    except progression.DecisionError as exc:
        return profile_page(request, user, employee_id, status_code=400, error=str(exc))
    role = progression.role_by_id(role_id)
    governance.record_audit(user["id"], user["role"], "career_assessment_assigned", f"/people/{employee['user_id']}",
                            details={"role_id": role_id, "cycle_id": cycle_id})
    engagement.notify([employee["user_id"]], "assessment", f"Readiness assessment assigned: {role['role_name']}",
                      "Your manager assigned it toward one of your career aspirations.", "/employee/dashboard")
    flash(request, f"Readiness assessment for {role['role_name']} assigned to {employee['full_name']} from SME-approved questions.")
    return _back(employee["user_id"], "career")


@router.post("/people/{employee_id}/career/nominate")
def career_nominate(request: Request, employee_id: str, role_id: Annotated[str, Form()] = "", note: Annotated[str, Form()] = "",
                    csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role(*VIEWERS))):
    security.validate_csrf(request, csrf_token)
    employee = _manager_of(user, employee_id, "career_nomination")
    role_id = _aspired_role(employee["user_id"], role_id)
    try:
        nomination_id = progression.nominate(employee["user_id"], user["id"], security.clean_form_text(note, 2000), role_id)
    except progression.DecisionError as exc:
        return profile_page(request, user, employee_id, status_code=400, error=str(exc))
    role = progression.role_by_id(role_id)
    governance.record_audit(user["id"], user["role"], "promotion_nominated", f"/people/{employee['user_id']}",
                            details={"nomination_id": nomination_id, "role_id": role_id})
    tower = (store.status_rows([employee["user_id"]]).get(employee["user_id"]) or {}).get("tower")
    engagement.notify(store.user_ids_with_role("leader"), "promotion", f"Promotion nomination: {employee['full_name']} → {role['role_name']}",
                      f"A move from {tower} into {role['tower']}." if tower and tower != role["tower"] else "",
                      f"/leader/promotions/{nomination_id}")
    flash(request, f"{employee['full_name']} nominated for {role['role_name']}: the leadership team decides.")
    return _back(employee["user_id"], "career")


# ---------------------------------------------------------------------------
# Development plan
# ---------------------------------------------------------------------------

@router.post("/people/{employee_id}/plan/generate")
def plan_generate(request: Request, employee_id: str, csrf_token: Annotated[str, Form()] = "",
                  user: dict = Depends(auth.require_role(*VIEWERS))):
    security.validate_csrf(request, csrf_token)
    employee = _manager_of(user, employee_id, "plan_generated")
    result = development.generate(employee["user_id"], user["id"], date.today())
    governance.record_audit(user["id"], user["role"], "plan_generated", f"/people/{employee['user_id']}", details=result)
    if result["created"]:
        engagement.notify([employee["user_id"]], "development", f"{result['created']} new development action(s)",
                          "Your manager updated your development plan.", "/employee/performance#development")
    if not result["created"]:
        message = "Nothing new to add: the plan already covers the current gaps."
    elif result["generated_by"] == "agent":
        message = f"Added {result['created']} development action(s): Agent 6 suggestions (verified) plus the generated plan."
    else:
        message = f"Added {result['created']} development action(s) from skill gaps, evidence and the latest review."
    flash(request, message)
    return _back(employee["user_id"], "development")


@router.post("/people/{employee_id}/actions")
def action_add(request: Request, employee_id: str, title: Annotated[str, Form()] = "",
               action_type: Annotated[str, Form()] = "", skill: Annotated[str, Form()] = "",
               due_date: Annotated[str, Form()] = "", detail: Annotated[str, Form()] = "",
               success_measure: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "",
               user: dict = Depends(auth.require_role(*VIEWERS))):
    security.validate_csrf(request, csrf_token)
    employee = _manager_of(user, employee_id, "action_added")
    try:
        development.add(employee["user_id"], title=title, action_type=action_type, skill=skill, due_date=due_date,
                        detail=detail, success_measure=success_measure, by=user["id"], today=date.today())
    except development.DevelopmentError as exc:
        return profile_page(request, user, employee_id, status_code=400, error=str(exc))
    engagement.notify([employee["user_id"]], "development", "New development action", security.clean_form_text(title, 140),
                      "/employee/performance#development")
    performance.refresh([employee["user_id"]])
    return _back(employee["user_id"], "development")


@router.post("/people/{employee_id}/actions/{action_id}/status")
def action_status(request: Request, employee_id: str, action_id: str, status: Annotated[str, Form()] = "",
                  csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role(*VIEWERS))):
    security.validate_csrf(request, csrf_token)
    employee = _manager_of(user, employee_id, "action_status")
    action = store.get_action(security.clean_form_text(action_id, 40))
    if not action or action["user_id"] != employee["user_id"] or status not in store.ACTION_STATUSES:
        raise HTTPException(404, "Action not found")
    store.set_action_status(action["action_id"], status)
    performance.refresh([employee["user_id"]])
    return _back(employee["user_id"], "development")


# ---------------------------------------------------------------------------
# Probation (manager side)
# ---------------------------------------------------------------------------

@router.post("/people/{employee_id}/probation/start")
def probation_start(request: Request, employee_id: str, start_date: Annotated[str, Form()] = "",
                    csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role(*VIEWERS))):
    security.validate_csrf(request, csrf_token)
    employee = _manager_of(user, employee_id, "probation_started")
    try:
        start = date.fromisoformat(start_date)
    except ValueError:
        return profile_page(request, user, employee_id, status_code=400, error="Choose the employee's start date.")
    if start > date.today() or (date.today() - start).days > probation.PROBATION_DAYS:
        return profile_page(request, user, employee_id, status_code=400,
                            error="Probation tracking is for people who started within the last six months.")
    store.upsert_profile(employee["user_id"], hire_date=start.isoformat())
    if not probation.start(employee["user_id"], start):
        return profile_page(request, user, employee_id, status_code=400, error="This employee already has a probation record.")
    governance.record_audit(user["id"], user["role"], "probation_started", f"/people/{employee['user_id']}")
    performance.refresh([employee["user_id"]])
    return _back(employee["user_id"], "probation")


@router.post("/people/{employee_id}/probation/checkpoints/{checkpoint_id}")
def probation_checkpoint(request: Request, employee_id: str, checkpoint_id: str, rating: Annotated[str, Form()] = "",
                         note: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "",
                         user: dict = Depends(auth.require_role(*VIEWERS))):
    security.validate_csrf(request, csrf_token)
    employee = _manager_of(user, employee_id, "probation_checkpoint")
    try:
        probation.complete_checkpoint(employee["user_id"], security.clean_form_text(checkpoint_id, 40), rating, note, user["id"])
    except probation.ProbationError as exc:
        return profile_page(request, user, employee_id, status_code=400, error=str(exc))
    governance.record_audit(user["id"], user["role"], "probation_checkpoint", f"/people/{employee['user_id']}",
                            details={"rating": rating})
    performance.refresh([employee["user_id"]])
    return _back(employee["user_id"], "probation")


@router.post("/people/{employee_id}/probation/recommend")
def probation_recommend(request: Request, employee_id: str, recommendation: Annotated[str, Form()] = "",
                        note: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "",
                        user: dict = Depends(auth.require_role(*VIEWERS))):
    security.validate_csrf(request, csrf_token)
    employee = _manager_of(user, employee_id, "probation_recommended")
    try:
        probation.recommend(employee["user_id"], recommendation, note, user["id"], date.today())
    except probation.ProbationError as exc:
        return profile_page(request, user, employee_id, status_code=400, error=str(exc))
    governance.record_audit(user["id"], user["role"], "probation_recommended", f"/people/{employee['user_id']}",
                            details={"recommendation": recommendation})
    performance.refresh([employee["user_id"]])
    flash(request, "Recommendation sent to the business leader for a decision.")
    return _back(employee["user_id"], "probation")


# ---------------------------------------------------------------------------
# Escalations
# ---------------------------------------------------------------------------

@router.post("/people/{employee_id}/escalations/{escalation_id}")
def escalation_update(request: Request, employee_id: str, escalation_id: str, status: Annotated[str, Form()] = "",
                      note: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "",
                      user: dict = Depends(auth.require_role(*VIEWERS))):
    security.validate_csrf(request, csrf_token)
    _not_read_only(user)
    employee = person_in_scope(user, employee_id, "escalation_update")
    item = store.get_escalation(security.clean_form_text(escalation_id, 40))
    if not item or item["user_id"] != employee["user_id"] or status not in ("acknowledged", "resolved"):
        raise HTTPException(404, "Escalation not found")
    if item["level"] == "leader" and user["role"] not in LEADERS:
        raise HTTPException(403, "Access restricted")
    note = security.clean_form_text(note, 1000)
    if status == "resolved" and len(note) < 10:
        return profile_page(request, user, employee_id, status_code=400, error="Record how the escalation was resolved.")
    store.set_escalation_status(item["escalation_id"], status, user["id"], note)
    governance.record_audit(user["id"], user["role"], f"escalation_{status}", f"/people/{employee['user_id']}",
                            details={"level": item["level"]})
    return _back(employee["user_id"], "escalations")


# ---------------------------------------------------------------------------
# Leader: performance overview and probation decisions
# ---------------------------------------------------------------------------

@router.get("/leader/performance")
def leader_performance(request: Request, user: dict = Depends(auth.require_role(*LEADERS))):
    today = date.today()
    scope = _scope_filters(user)
    performance.refresh_missing(today)
    overview = performance.org_overview(scope)
    concerns = performance.directory({**scope, "signal": "performance_concern"}, 1, "attention", 8)
    ready = performance.directory({**scope, "signal": "promotion_ready"}, 1, "score_desc", 8)
    best = performance.directory({**scope, "band": "Exceeding"}, 1, "score_desc", 8)
    in_scope = _scope_ids(user)
    ids = None if in_scope is None else sorted(in_scope)
    return render(request, "leader/performance.html", overview=overview, concerns=concerns, ready=ready, best=best,
                  escalations=store.list_escalations(user_ids=ids, level="leader", limit=25),
                  escalation_counts=store.escalation_counts(ids),
                  probation=probation.summary(in_scope, today), notice=pop_flash(request))


def probation_page(request: Request, user: dict, status_code: int = 200, error: str | None = None):
    return render(request, "leader/probation.html", status_code=status_code,
                  pipeline=probation.pipeline(_scope_ids(user), date.today()), recommendations=probation.RECOMMENDATIONS,
                  notice=pop_flash(request), error=error)


@router.get("/leader/probation")
def leader_probation(request: Request, user: dict = Depends(auth.require_role(*ORG_VIEWERS))):
    return probation_page(request, user)


@router.post("/leader/probation/{probation_id}/decision")
def leader_probation_decision(request: Request, probation_id: str, decision: Annotated[str, Form()] = "",
                              note: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "",
                              user: dict = Depends(auth.require_role(*LEADERS))):
    security.validate_csrf(request, csrf_token)
    record = store.get_probation(security.clean_form_text(probation_id, 40))
    if not record:
        raise HTTPException(404, "Probation not found")
    person_in_scope(user, record["user_id"], "probation_decision")
    try:
        result = probation.decide(record["probation_id"], decision, note, user["id"])
    except probation.ProbationError as exc:
        return probation_page(request, user, status_code=400, error=str(exc))
    governance.record_audit(user["id"], user["role"], f"probation_{result['outcome']}", f"/leader/probation/{probation_id}")
    performance.refresh([record["user_id"]])
    flash(request, f"Decision recorded: {result['outcome'].replace('_', ' ')}. The manager has been notified.")
    return RedirectResponse("/leader/probation", status_code=303)
