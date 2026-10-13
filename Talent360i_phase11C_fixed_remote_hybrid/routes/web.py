"""Shared web plumbing for app.py and the route modules: templates, render(),
and who-can-see-whom scopes."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

from domain import frameworks, governance
from services import auth, db, lifecycle_store, progression, security

BASE_DIR = Path(__file__).resolve().parents[1]
DEBUG_MODE = os.getenv("TALENT360_DEBUG", "true").lower() in {"1", "true", "yes", "development"}

templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.filters["decision_label"] = progression.decision_label
# {"band": "", "manager": "M001"}|query -> "manager=M001": a query string without the empty filters.
templates.env.filters["query"] = lambda params: urlencode({k: v for k, v in params.items() if v not in (None, "")})


def _unread(user: dict | None) -> int:
    if not user:
        return 0
    try:
        return lifecycle_store.unread_count(user["id"])
    except sqlite3.Error:
        return 0  # render() also draws the error pages: a database fault must not break them


def render(request: Request, template_name: str, status_code: int = 200, **context):
    user = auth.get_current_user(request)
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        status_code=status_code,
        context={
            "current_user": user,
            "debug_mode": DEBUG_MODE,
            "role_labels": auth.USER_ROLES,
            "landing_path": auth.landing_path(user) if user else "/login",
            "frameworks": frameworks.framework_options(),
            "csrf_token": security.ensure_csrf_token(request),
            "unread_notifications": _unread(user),
            **context,
        },
    )


def employee_scope(user: dict) -> str:
    return "U001" if user["role"] == "admin" else user["id"]


def manager_scope(user: dict) -> str:
    # Admins see the demo manager's team, mirroring employee_scope().
    return "M001" if user["role"] == "admin" else user["id"]


def leader_scope(user: dict) -> list[str] | None:
    """Department ids a leader is responsible for, or None for the whole organisation
    (admins, and leaders who don't lead a specific department yet)."""
    if user["role"] != "leader":
        return None
    return lifecycle_store.departments_led_by(user["id"]) or None


def person_in_scope(user: dict, employee_id: str, action: str) -> dict:
    """Resource-level access to one employee's lifecycle record: the line manager,
    a leader whose departments include them (or any leader when unscoped), an HR
    partner or an admin (organisation-wide)."""
    employee = db.get_user(security.clean_form_text(employee_id, 80))
    allowed = False
    if employee and employee["role"] == "employee":
        if user["role"] in ("admin", "hr"):
            allowed = True
        elif user["role"] == "manager":
            allowed = employee.get("manager_id") == user["id"]
        elif user["role"] == "leader":
            depts = leader_scope(user)
            profile = lifecycle_store.get_profile(employee["user_id"]) or {}
            allowed = depts is None or profile.get("dept_id") in depts
    if not allowed:
        governance.record_audit(user["id"], user["role"], action, f"/people/{employee_id}", outcome="denied")
        raise HTTPException(404, "Employee not found")
    return employee


def flash(request: Request, message: str) -> None:
    request.session["flash"] = message


def pop_flash(request: Request):
    return request.session.pop("flash", None)
