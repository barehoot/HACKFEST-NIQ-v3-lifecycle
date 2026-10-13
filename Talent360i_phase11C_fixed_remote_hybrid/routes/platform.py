"""Platform routes: the notification inbox, SME coding challenges and their
review queue, the evidence-ingestion endpoint for Outlook / Teams flows, and
health probes for the load balancer and container platform."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from domain import governance
from services import auth, coding, db, evidence, lifecycle_store as store, performance, security
from routes.web import flash, pop_flash, render

router = APIRouter()
EVERYONE = tuple(auth.USER_ROLES)  # every signed-in role, including roles added later (hr)
MAX_INGEST_BYTES = 64 * 1024


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

@router.get("/notifications")
def notifications(request: Request, user: dict = Depends(auth.require_role(*EVERYONE))):
    return render(request, "notifications.html", items=store.list_notifications(user["id"], 100), notice=pop_flash(request))


@router.post("/notifications/read")
def notifications_read(request: Request, csrf_token: Annotated[str, Form()] = "",
                       user: dict = Depends(auth.require_role(*EVERYONE))):
    security.validate_csrf(request, csrf_token)
    store.mark_notifications_read(user["id"])
    return RedirectResponse("/notifications", status_code=303)


@router.post("/notifications/{notification_id}/open")
def notification_open(request: Request, notification_id: str, csrf_token: Annotated[str, Form()] = "",
                      user: dict = Depends(auth.require_role(*EVERYONE))):
    security.validate_csrf(request, csrf_token)
    notification_id = security.clean_form_text(notification_id, 40)
    item = next((n for n in store.list_notifications(user["id"], 500) if n["notification_id"] == notification_id), None)
    if not item:
        raise HTTPException(404, "Notification not found")
    store.mark_notifications_read(user["id"], notification_id)
    link = item.get("link") or "/notifications"
    # Stored links are in-app paths; anything else falls back to the inbox (no open redirect).
    return RedirectResponse(link if link.startswith("/") and not link.startswith("//") else "/notifications", status_code=303)


# ---------------------------------------------------------------------------
# SME coding challenges
# ---------------------------------------------------------------------------

def coding_page(request: Request, status_code: int = 200, error: str | None = None):
    return render(request, "sme/coding.html", status_code=status_code, challenges=store.list_challenges(),
                  attempts=store.list_attempts(limit=100), languages=store.CODING_LANGUAGES,
                  difficulties=coding.DIFFICULTIES, error=error, notice=pop_flash(request))


@router.get("/sme/coding")
def sme_coding(request: Request, user: dict = Depends(auth.require_role("sme", "admin"))):
    return coding_page(request)


@router.post("/sme/coding")
def sme_coding_create(request: Request, title: Annotated[str, Form()] = "", prompt: Annotated[str, Form()] = "",
                      language: Annotated[str, Form()] = "", starter_code: Annotated[str, Form()] = "",
                      keywords: Annotated[str, Form()] = "", skills: Annotated[str, Form()] = "",
                      difficulty: Annotated[str, Form()] = "intermediate", rubric: Annotated[str, Form()] = "",
                      time_minutes: Annotated[int, Form()] = 30, csrf_token: Annotated[str, Form()] = "",
                      user: dict = Depends(auth.require_role("sme", "admin"))):
    security.validate_csrf(request, csrf_token)
    try:
        challenge_id = coding.create(title=title, prompt=prompt, language=language, starter_code=starter_code,
                                     keywords=keywords, skills=skills, difficulty=difficulty, rubric=rubric,
                                     time_minutes=time_minutes, created_by=user["id"])
    except coding.CodingError as exc:
        return coding_page(request, status_code=400, error=str(exc))
    governance.record_audit(user["id"], user["role"], "coding_challenge_created", "/sme/coding",
                            details={"challenge_id": challenge_id})
    flash(request, "Challenge saved as a draft. Approve it to start triggering it from resumes.")
    return RedirectResponse("/sme/coding", status_code=303)


@router.post("/sme/coding/{challenge_id}/status")
def sme_coding_status(request: Request, challenge_id: str, status: Annotated[str, Form()] = "",
                      csrf_token: Annotated[str, Form()] = "", user: dict = Depends(auth.require_role("sme", "admin"))):
    security.validate_csrf(request, csrf_token)
    if status not in ("approved", "retired", "draft"):
        raise HTTPException(400, "Unknown status")
    if not store.set_challenge_status(security.clean_form_text(challenge_id, 40), status, user["id"]):
        raise HTTPException(404, "Challenge not found")
    governance.record_audit(user["id"], user["role"], f"coding_challenge_{status}", f"/sme/coding/{challenge_id}")
    return RedirectResponse("/sme/coding", status_code=303)


@router.post("/sme/coding/attempts/{attempt_id}/review")
def sme_coding_review(request: Request, attempt_id: str, score: Annotated[int, Form()] = 0,
                      note: Annotated[str, Form()] = "", csrf_token: Annotated[str, Form()] = "",
                      user: dict = Depends(auth.require_role("sme", "admin"))):
    security.validate_csrf(request, csrf_token)
    try:
        ok = coding.sme_review(security.clean_form_text(attempt_id, 40), score, note, user["id"])
    except coding.CodingError as exc:
        return coding_page(request, status_code=400, error=str(exc))
    if not ok:
        raise HTTPException(404, "Attempt not found")
    governance.record_audit(user["id"], user["role"], "coding_attempt_reviewed", f"/sme/coding/attempts/{attempt_id}",
                            details={"score": score})
    flash(request, "Score recorded. The manager sees it on the validation page.")
    return RedirectResponse("/sme/coding#attempts", status_code=303)


# ---------------------------------------------------------------------------
# Evidence ingestion for Outlook / Teams flows (Power Automate or a Graph job)
# ---------------------------------------------------------------------------

def _token_ok(supplied: str, expected: str) -> bool:
    return bool(supplied and expected) and hmac.compare_digest(
        hashlib.sha256(supplied.encode()).digest(), hashlib.sha256(expected.encode()).digest())


@router.post("/integrations/evidence")
async def integration_evidence(request: Request):
    """A manager tags a message (Outlook category "Talent360i evidence", or a Teams message action);
    the flow posts it here. It lands as a pending suggestion the manager confirms -- nothing is
    recorded about anyone without that confirmation. Disabled unless TALENT360_INTEGRATION_TOKEN is set."""
    expected = os.getenv("TALENT360_INTEGRATION_TOKEN", "")
    if not expected:
        return JSONResponse(status_code=404, content={"message": "Not found"})
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not _token_ok(supplied.strip(), expected):
        governance.record_audit("integration", "integration", "evidence_ingest", "/integrations/evidence", outcome="denied")
        return JSONResponse(status_code=401, content={"message": "Authentication required"})
    body = await request.body()
    if len(body) > MAX_INGEST_BYTES:
        return JSONResponse(status_code=413, content={"message": "Message too large"})
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return JSONResponse(status_code=400, content={"message": "Body must be JSON"})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"message": "Body must be a JSON object"})
    try:
        evidence_id = evidence.ingest(payload, date.today())
    except LookupError:
        return JSONResponse(status_code=404, content={"message": "Unknown employee or tagger"})
    except PermissionError:
        governance.record_audit("integration", "integration", "evidence_ingest", "/integrations/evidence",
                                outcome="denied", details={"reason": "tagger is not the manager"})
        return JSONResponse(status_code=403, content={"message": "Only the employee's manager can tag evidence"})
    except evidence.EvidenceError as exc:
        return JSONResponse(status_code=400, content={"message": str(exc)})
    governance.record_audit("integration", "integration", "evidence_ingested", "/integrations/evidence",
                            details={"evidence_id": evidence_id, "source": payload.get("source")})
    return JSONResponse(status_code=201, content={"evidence_id": evidence_id, "status": "pending_manager_confirmation"})


# ---------------------------------------------------------------------------
# Health probes (no auth, no detail)
# ---------------------------------------------------------------------------

@router.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
def readyz():
    try:
        db.get_user("__readiness_probe__")
    except Exception:  # any database fault means "not ready"
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ready"}
