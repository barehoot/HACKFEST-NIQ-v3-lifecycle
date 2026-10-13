"""Internal job postings from SmartRecruiters, the internal job portal.

Connected mode (SMARTRECRUITERS_COMPANY_ID set) reads the SmartRecruiters Posting API:

    GET {base}/v1/companies/{company}/postings?limit=100&offset=N[&destination=...]
    GET {base}/v1/companies/{company}/postings/{postingId}        (job ad sections)

with the optional X-SmartToken header (SMARTRECRUITERS_API_TOKEN) for a tenant that
restricts internal postings. Without a company id the portal uses the bundled demo
postings (data/smartrecruiters_demo_postings.json, same field shape) and says so.

Every posting is cached in job_postings and mapped to a job profile: automatically by
title, or by an admin / HR partner (a manual mapping survives later syncs). A mapped
posting gets the job profile's skill requirements, so employees see their readiness for
it and hiring managers get a talent pool; an unmapped one can be onboarded as a new job
profile from its JD.

Security: https only, host must be *.smartrecruiters.com (or the configured base), 10 s
timeout, 2 MB response cap, JSON only; job-ad HTML is reduced to text and sanitised; the
token is never logged or shown.
"""
from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from domain.governance import sanitise_text
from services import db, lifecycle_store as store

DEMO_FILE = Path(__file__).resolve().parents[2] / "data" / "smartrecruiters_demo_postings.json"
MAX_BYTES = 2 * 1024 * 1024
MAX_POSTINGS = 300
MAX_DETAILS = 150
TIMEOUT = 10
STALE_MINUTES = 30


class SmartRecruitersError(RuntimeError):
    pass


def settings() -> dict:
    return {"company": os.getenv("SMARTRECRUITERS_COMPANY_ID", "").strip(),
            "base": os.getenv("SMARTRECRUITERS_API_BASE", "https://api.smartrecruiters.com").rstrip("/"),
            "token": os.getenv("SMARTRECRUITERS_API_TOKEN", "").strip(),
            "destination": os.getenv("SMARTRECRUITERS_DESTINATION", "").strip()}


def configured() -> bool:
    return bool(settings()["company"])


def _check_base(base: str) -> None:
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.hostname:
        raise SmartRecruitersError("SMARTRECRUITERS_API_BASE must be an https URL.")
    if not (parts.hostname == "smartrecruiters.com" or parts.hostname.endswith(".smartrecruiters.com")):
        raise SmartRecruitersError("SMARTRECRUITERS_API_BASE must be a smartrecruiters.com host.")


def _get(path: str, params: dict | None = None) -> dict:
    cfg = settings()
    _check_base(cfg["base"])
    url = f"{cfg['base']}{path}" + (f"?{urlencode(params)}" if params else "")
    headers = {"Accept": "application/json", "User-Agent": "Talent360i/3.2"}
    if cfg["token"]:
        headers["X-SmartToken"] = cfg["token"]
    try:
        # https + smartrecruiters.com host enforced by _check_base (bandit B310 reviewed).
        with urlopen(Request(url, headers=headers, method="GET"), timeout=TIMEOUT) as response:  # nosec B310
            raw = response.read(MAX_BYTES + 1)
    except HTTPError as exc:
        raise SmartRecruitersError(f"SmartRecruiters returned HTTP {exc.code}.") from exc
    except (URLError, TimeoutError) as exc:
        raise SmartRecruitersError("SmartRecruiters could not be reached.") from exc
    if len(raw) > MAX_BYTES:
        raise SmartRecruitersError("SmartRecruiters response too large.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmartRecruitersError("SmartRecruiters returned something other than JSON.") from exc
    if not isinstance(data, dict):
        raise SmartRecruitersError("Unexpected SmartRecruiters response.")
    return data


_TAG_RE = re.compile(r"<[^>]{0,2000}>")
_BLOCK_RE = re.compile(r"(?is)<(script|style|head)\b.*?</\1\s*>")


def html_text(markup: str | None, limit: int) -> str:
    text = _BLOCK_RE.sub(" ", str(markup or ""))
    text = re.sub(r"(?i)<\s*(br|/p|/li|/h\d)\s*/?>", "\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines = (sanitise_text(line, 400) for line in text.splitlines())
    return "\n".join(line for line in lines if line)[:limit]


def parse_posting(raw: dict, detail: dict | None, source: str) -> dict | None:
    """One Posting API item (+ its detail) -> a job_postings row. Field mapping lives here only."""
    posting_id = sanitise_text(str(raw.get("id") or raw.get("uuid") or ""), 80)
    title = sanitise_text(raw.get("name") or raw.get("title"), 160)
    if not posting_id or not title:
        return None
    location = raw.get("location") or {}
    place = ", ".join(x for x in (location.get("city"), location.get("region"), location.get("country")) if x)
    if location.get("remote"):
        place = f"{place} (remote)" if place else "Remote"
    sections = ((detail or raw).get("jobAd") or {}).get("sections") or {}
    company = sanitise_text((raw.get("company") or {}).get("identifier") or settings()["company"], 80)
    apply_url = raw.get("postingUrl") or raw.get("applyUrl") or (
        f"https://jobs.smartrecruiters.com/{quote(company)}/{quote(posting_id)}" if company else None)
    if apply_url and not str(apply_url).startswith("https://"):
        apply_url = None
    return {
        "posting_id": f"SR-{posting_id}" if source == "smartrecruiters" and not posting_id.startswith("SR-") else posting_id,
        "source": source, "title": title,
        "department": sanitise_text((raw.get("department") or {}).get("label") or (raw.get("function") or {}).get("label"), 120) or None,
        "location": sanitise_text(place, 160) or None,
        "experience_level": sanitise_text((raw.get("experienceLevel") or {}).get("label"), 60) or None,
        "employment_type": sanitise_text((raw.get("typeOfEmployment") or {}).get("label"), 60) or None,
        "released_on": sanitise_text(str(raw.get("releasedDate") or ""), 10) or None,
        "apply_url": sanitise_text(apply_url, 400) if apply_url else None,
        "description": html_text((sections.get("jobDescription") or {}).get("text"), 4000) or None,
        "qualifications": html_text((sections.get("qualifications") or {}).get("text"), 3000) or None,
    }


def auto_map(title: str) -> str | None:
    """A job profile with the same title, or whose title the posting's contains (longest wins)."""
    clean = re.sub(r"\s+", " ", title.lower()).strip()
    roles = db.role_master_rows()
    exact = next((r for r in roles if (r["role_name"] or "").strip().lower() == clean), None)
    if exact:
        return exact["role_id"]
    contained = [r for r in roles if r["role_name"] and len(r["role_name"]) >= 6 and r["role_name"].strip().lower() in clean]
    return max(contained, key=lambda r: len(r["role_name"]))["role_id"] if contained else None


def _fetch_live() -> list[dict]:
    cfg = settings()
    base_path = f"/v1/companies/{quote(cfg['company'])}/postings"
    items, offset = [], 0
    while len(items) < MAX_POSTINGS:
        params = {"limit": 100, "offset": offset}
        if cfg["destination"]:
            params["destination"] = cfg["destination"]
        page = _get(base_path, params)
        content = page.get("content") if isinstance(page.get("content"), list) else []
        items += [c for c in content if isinstance(c, dict)]
        offset += len(content)
        if not content or offset >= int(page.get("totalFound") or 0):
            break
    out = []
    for n, raw in enumerate(items[:MAX_POSTINGS]):
        detail = None
        if n < MAX_DETAILS and raw.get("id"):
            try:
                detail = _get(f"{base_path}/{quote(str(raw['id']))}")
            except SmartRecruitersError:
                detail = None  # the list entry is still useful without its job ad
        parsed = parse_posting(raw, detail, "smartrecruiters")
        if parsed:
            out.append(parsed)
    return out


def _fetch_demo() -> list[dict]:
    try:
        data = json.loads(DEMO_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for raw in data.get("content", []):
        parsed = parse_posting(raw, None, "demo")
        if parsed:
            # Demo postings are dated relative to today, so the list never looks stale.
            days = int(raw.get("releasedDaysAgo") or 7)
            parsed["released_on"] = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
            parsed["hiring_manager_id"] = raw.get("hiringManagerId") if db.get_user(raw.get("hiringManagerId") or "") else None
            out.append(parsed)
    return out


def sync(force: bool = False) -> dict:
    """Refreshes the cache (at most every 30 minutes unless forced). Returns {"source", "count", "mapped", "error"}."""
    state = store.posting_sync_state()
    source = "smartrecruiters" if configured() else "demo"
    if not force and state["source"] == source and state["fetched_at"]:
        fetched = datetime.fromisoformat(state["fetched_at"])
        if datetime.now(timezone.utc) - fetched < timedelta(minutes=STALE_MINUTES):
            return {"source": source, "count": state["open"], "mapped": None, "error": None}
    error = None
    try:
        postings = _fetch_live() if source == "smartrecruiters" else _fetch_demo()
    except SmartRecruitersError as exc:
        postings, error = None, str(exc)
    if postings is None:
        return {"source": source, "count": state["open"], "mapped": None, "error": error}
    mapped = 0
    for posting in postings:
        role_id = auto_map(posting["title"])
        mapped += bool(role_id)
        store.upsert_posting({**posting, "role_id": role_id, "mapped_by": "auto" if role_id else None})
    store.close_postings_except(source, [p["posting_id"] for p in postings])
    return {"source": source, "count": len(postings), "mapped": mapped, "error": None}


def ensure_loaded() -> None:
    """Pages call this: it fetches only when nothing has been fetched from the current source yet.
    Keeping the feed fresh is the lifecycle worker's job (scripts/lifecycle_worker.py)."""
    if store.posting_sync_state()["source"] != ("smartrecruiters" if configured() else "demo"):
        sync(force=True)


def search(q: str = "", tower: str = "") -> list[dict]:
    ensure_loaded()
    q = q.strip().lower()
    out = []
    for posting in store.list_postings():
        haystack = " ".join(str(posting.get(k) or "") for k in ("title", "department", "location", "description")).lower()
        if q and q not in haystack:
            continue
        if tower and posting.get("tower") != tower:
            continue
        out.append(posting)
    return out
