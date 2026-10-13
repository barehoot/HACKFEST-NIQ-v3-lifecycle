"""Presentation-layer security helpers for Talent 360i."""
import os
import re
import threading
import time
from collections import defaultdict
from pathlib import Path

from fastapi import HTTPException, Request, status
from domain.governance import csrf_valid, sanitise_text

CSRF_SESSION_KEY = "csrf_token"

MAX_UPLOAD_BYTES = int(os.getenv("TALENT360_MAX_UPLOAD_BYTES", str(5 * 1024 * 1024)))
ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".docx", ".txt"}


def ensure_csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_SESSION_KEY)
    if not token:
        from domain.governance import new_csrf_token
        token = new_csrf_token()
        request.session[CSRF_SESSION_KEY] = token
    return token


def validate_csrf(request: Request, supplied: str | None) -> None:
    if not csrf_valid(request.session.get(CSRF_SESSION_KEY), supplied):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or missing CSRF token")


def clean_form_text(value: str | None, limit: int = 2000) -> str:
    return sanitise_text(value, limit)


def clean_multiline_text(value: str | None, limit: int = 3000) -> str:
    """Like clean_form_text, but keeps paragraph breaks (free-text simulation responses)."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text[:limit]


# ---------------------------------------------------------------------------
# Upload validation (guideline A.2 / B.4): allowlist, size cap, magic bytes.
# The backend re-validates; this rejects bad files before they leave the portal.
# ---------------------------------------------------------------------------

def validate_upload(filename: str | None, data: bytes) -> str:
    """Returns a safe filename, or raises ValueError with a user-safe message."""
    base = os.path.basename((filename or "").replace("\\", "/"))
    name = re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".")[:100] or "resume"
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        raise ValueError(f"Unsupported file type. Allowed: {', '.join(sorted(ALLOWED_UPLOAD_EXTENSIONS))}.")
    if not data:
        raise ValueError("The uploaded file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"File is too large (limit {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")
    if ext == ".pdf" and data.lstrip()[:5] != b"%PDF-":
        raise ValueError("File content does not match a PDF document.")
    if ext == ".docx" and data[:4] != b"PK\x03\x04":
        raise ValueError("File content does not match a DOCX document.")
    if ext == ".txt" and b"\x00" in data[:4096]:
        raise ValueError("File content does not look like plain text.")
    return name


# ---------------------------------------------------------------------------
# Password policy (guideline A.4 / B.2) -- applied when admins create accounts.
# ---------------------------------------------------------------------------

_WEAK_FRAGMENTS = ("password", "passw0rd", "qwerty", "123456", "letmein", "welcome", "admin123", "talent360")


def validate_password(password: str, username: str = "") -> None:
    """Raises ValueError with a user-safe message when the password is too weak."""
    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters long.")
    if len(password) > 128:
        raise ValueError("Password must be at most 128 characters long.")
    classes = sum(bool(re.search(p, password)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    if classes < 3:
        raise ValueError("Password must mix at least three of: lowercase, uppercase, digits, symbols.")
    lowered = password.lower()
    if any(f in lowered for f in _WEAK_FRAGMENTS) or (username and username.lower() in lowered):
        raise ValueError("Password is too easy to guess.")


_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,59}$")
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


def validate_username(username: str) -> str:
    username = username.strip().lower()
    if not _USERNAME_RE.match(username):
        raise ValueError("Username must be 3-60 characters: letters, digits, '.', '_' or '-'.")
    return username


def validate_email(email: str) -> str:
    email = email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise ValueError("Enter a valid email address.")
    return email


# ---------------------------------------------------------------------------
# Login throttling (guideline C.7: rate limiting against auth weaknesses).
# In-memory, per (client ip, username): 5 failures -> 5 minute lockout.
# ---------------------------------------------------------------------------

MAX_LOGIN_FAILURES = 5
LOCKOUT_SECONDS = 300
_failures: dict[str, list[float]] = defaultdict(list)
_failures_lock = threading.Lock()


def _login_key(request: Request, username: str) -> str:
    ip = request.client.host if request.client else "unknown"
    return f"{ip}|{username.strip().lower()[:60]}"


def login_locked(request: Request, username: str) -> bool:
    key = _login_key(request, username)
    now = time.monotonic()
    with _failures_lock:
        recent = [t for t in _failures[key] if now - t < LOCKOUT_SECONDS]
        _failures[key] = recent
        return len(recent) >= MAX_LOGIN_FAILURES


def record_login_failure(request: Request, username: str) -> None:
    with _failures_lock:
        _failures[_login_key(request, username)].append(time.monotonic())


def clear_login_failures(request: Request, username: str) -> None:
    with _failures_lock:
        _failures.pop(_login_key(request, username), None)
