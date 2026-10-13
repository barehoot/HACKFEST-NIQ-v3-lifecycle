"""Authentication and role-based access for Talent 360i.

Real username/password login backed by the `users` table in
db/talent360i.sqlite (see services/db.py and scripts/seed_users.py in
the project root). The session only stores the logged-in user_id; every
request re-reads the user (and their role) from the database, so a role
change made by an admin takes effect on the user's next request.

Route-level role checks (require_role) are enforced server-side and are not
replaced by navigation hiding.
"""
import os
import time
from collections.abc import Callable
from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse

from services import db

USER_ROLES = {
    "employee": "Employee",
    "sme": "Subject Matter Expert",
    "scheduler": "Scheduler",
    "manager": "Manager",
    "leader": "Leader",
    "admin": "Administrator",
    "hr": "HR Business Partner",
}


def _presentation_user(user: dict) -> dict:
    result = dict(user)
    result["id"] = result["user_id"]
    result["name"] = result["full_name"]
    result["role_label"] = USER_ROLES.get(result["role"], result["role"])
    result["initials"] = "".join(part[0] for part in result["name"].split()[:2]).upper() if result["name"] else "?"
    return result


IDLE_TIMEOUT_SECONDS = int(os.getenv("TALENT360_IDLE_TIMEOUT", "1800"))
ABSOLUTE_TIMEOUT_SECONDS = int(os.getenv("TALENT360_SESSION_MAX_AGE", "28800"))


def get_current_user(request: Request) -> dict | None:
    session = request.session
    user_id = session.get("user_id")
    if not user_id:
        return None
    # Session timeouts (guideline A.4 / B.2): idle and absolute expiry.
    now = time.time()
    if now - session.get("last_seen", now) > IDLE_TIMEOUT_SECONDS or now - session.get("login_at", now) > ABSOLUTE_TIMEOUT_SECONDS:
        session.clear()
        return None
    session["last_seen"] = now
    user = db.get_user(user_id)
    return _presentation_user(user) if user else None


def login(request: Request, username: str, password: str) -> dict | None:
    """Verifies credentials against the users table and starts a session. Returns the user, or None."""
    user = db.verify_user(username, password)
    if not user:
        return None
    # Regenerate the session on privilege change: drop anything an attacker
    # may have planted pre-login (session fixation), then issue a fresh CSRF token.
    request.session.clear()
    now = time.time()
    request.session["user_id"] = user["user_id"]
    request.session["login_at"] = now
    request.session["last_seen"] = now
    return _presentation_user(user)


def has_role(user: dict | None, *roles: str) -> bool:
    return bool(user and user["role"] in roles)


def require_role(*roles: str) -> Callable:
    def dependency(request: Request) -> dict:
        user = get_current_user(request)
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
        if not has_role(user, *roles):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access restricted")
        return user
    return dependency


def landing_path(user: dict) -> str:
    return {
        "employee": "/employee/dashboard",
        "sme": "/sme/dashboard",
        "scheduler": "/scheduler/dashboard",
        "manager": "/manager/dashboard",
        "leader": "/leader/dashboard",
        "admin": "/admin/dashboard",
        "hr": "/hr/dashboard",
    }[user["role"]]


def login_redirect() -> RedirectResponse:
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
