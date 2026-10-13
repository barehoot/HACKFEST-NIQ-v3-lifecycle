"""Performance evidence: documented facts tagged to an employee.

Where evidence comes from:
  manual       the manager logs an appreciation, achievement, feedback, 1:1, escalation or concern
  outlook      an email the manager saved (.eml) or pasted
  teams        a Teams conversation the manager pasted or exported (.txt)
  peer         a colleague's recognition (services/engagement.send_kudos)
  integration  a Power Automate / Microsoft Graph flow posting a message the manager
               tagged with the "Talent360i evidence" Outlook category or Teams action

Human in the loop: anything the manager did not write themselves starts as a
pending suggestion. Agent 7 proposes the kind, sentiment, skills and a factual
one-line summary; the manager confirms (editing as needed) or dismisses it.
Only confirmed evidence counts, and only evidence shared with the employee moves
their status -- the employee can see it and add their side.

Privacy: no mailbox is ever scanned -- only messages a manager deliberately
tags reach Talent 360i. The platform keeps a redacted excerpt (emails, phone
numbers and long number runs masked, at most 1,500 characters), never the raw
message or its attachments, plus a content hash so the same message can't be
counted twice.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import date, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from domain.governance import sanitise_text
from services import api_client, db, development, engagement, lifecycle_store as store, tni
from services.backend_api import BackendAPIError
from services.security import clean_multiline_text

MAX_IMPORT_BYTES = 1024 * 1024
ALLOWED_IMPORT_EXTENSIONS = {".eml", ".txt"}
EXCERPT_CHARS = 1500
CLASSIFY_CHARS = 4000
KIND_SENTIMENT = {"appreciation": "positive", "achievement": "positive", "feedback": "neutral",
                  "one_on_one": "neutral", "escalation": "negative", "concern": "negative"}
KIND_LABELS = {"appreciation": "Appreciation", "achievement": "Achievement", "feedback": "Feedback",
               "one_on_one": "1:1 discussion", "escalation": "Escalation", "concern": "Concern"}
SOURCE_LABELS = {"manual": "Logged by manager", "outlook": "Outlook email", "teams": "Teams conversation",
                 "email_file": "Email file", "peer": "Peer recognition", "integration": "Outlook / Teams flow"}
ACTION_ITEM_DAYS = 14

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\(?\d[\d\s().-]{7,}\d")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_LONG_NUMBER_RE = re.compile(r"\b\d{9,}\b")
_TAG_RE = re.compile(r"<[^>]{0,2000}>")
_BLOCK_RE = re.compile(r"(?is)<(script|style|head)\b.*?</\1\s*>")


class EvidenceError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Parsing and redaction
# ---------------------------------------------------------------------------

def redact(text: str) -> str:
    text = _EMAIL_RE.sub("[email]", text or "")

    def phone(match):
        value = match.group(0)
        digits = sum(ch.isdigit() for ch in value)
        formatted = value.startswith("+") or any(ch in value for ch in " ().-")  # a bare digit run is an account number
        return "[phone]" if formatted and digits >= 10 and not _ISO_DATE_RE.match(value.lstrip("+(")) else value

    text = _PHONE_RE.sub(phone, text)
    return _LONG_NUMBER_RE.sub("[number]", text)


def html_to_text(markup: str) -> str:
    text = _BLOCK_RE.sub(" ", markup or "")
    text = re.sub(r"(?i)<\s*(br|/p|/div|/li|/tr)\b[^>]*>", "\n", text)
    return html.unescape(_TAG_RE.sub(" ", text))


def parse_eml(data: bytes) -> dict:
    """Subject, sender, date and plain-text body of an Outlook .eml. Attachments are ignored."""
    try:
        message = BytesParser(policy=policy.default).parsebytes(data)
    except Exception as exc:  # hostile/malformed MIME: the parser can raise many types
        raise EvidenceError("The file could not be read as an email.") from exc
    body_part = message.get_body(preferencelist=("plain", "html"))
    if body_part is None:
        raise EvidenceError("The email has no readable text.")
    try:
        content = body_part.get_content()
    except (LookupError, ValueError) as exc:
        raise EvidenceError("The email body could not be decoded.") from exc
    if body_part.get_content_type() == "text/html":
        content = html_to_text(content)
    sent_on = None
    if message.get("date"):
        try:
            sent_on = parsedate_to_datetime(str(message["date"])).date().isoformat()
        except (TypeError, ValueError):
            sent_on = None
    name, address = parseaddr(str(message.get("from") or ""))
    return {"subject": str(message.get("subject") or ""), "sender": name or address.split("@")[0],
            "sent_on": sent_on, "body": content}


def parse_upload(filename: str | None, data: bytes) -> dict:
    ext = Path((filename or "").lower()).suffix
    if ext not in ALLOWED_IMPORT_EXTENSIONS:
        raise EvidenceError("Upload an Outlook email saved as .eml, or a Teams conversation exported as .txt.")
    if not data:
        raise EvidenceError("The file is empty.")
    if len(data) > MAX_IMPORT_BYTES:
        raise EvidenceError("The file is larger than 1 MB. Save the single message, without attachments.")
    if b"\x00" in data[:4096]:
        raise EvidenceError("The file does not look like an email or text export.")
    if ext == ".eml":
        return {**parse_eml(data), "source": "outlook"}
    return {"subject": "", "sender": None, "sent_on": None, "body": data.decode("utf-8", errors="replace"),
            "source": "teams"}


def _content_hash(user_id: str, text: str) -> str:
    normal = " ".join(text.lower().split())
    return hashlib.sha256(f"{user_id}|{normal}".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Classification (Agent 7, with the manager confirming)
# ---------------------------------------------------------------------------

def role_skills(user_id: str) -> list[str]:
    role = tni.current_role(user_id)
    return [s["skill"] for s in db.role_skills(role["role_id"])] if role else []


def classify(text: str, skills: list[str], source: str) -> dict:
    """Agent 7's suggestion. If the pipeline API is unreachable the item still
    lands as pending, unclassified, for the manager to tag by hand (or, for a
    manual note, simply without an AI reading)."""
    try:
        result = api_client.backend_classify_evidence(text[:CLASSIFY_CHARS], skills[:20], source) or {}
    except BackendAPIError:
        result = {}
    kind = result.get("kind") if result.get("kind") in KIND_SENTIMENT else "feedback"
    sentiment = result.get("sentiment") if result.get("sentiment") in ("positive", "neutral", "negative") else KIND_SENTIMENT[kind]
    allowed = {s.lower(): s for s in skills}
    return {
        "kind": kind, "sentiment": sentiment,
        "summary": sanitise_text(result.get("summary") or "", 300),
        "skills": [allowed[s.lower()] for s in result.get("skills") or [] if isinstance(s, str) and s.lower() in allowed][:4],
        "action_items": [sanitise_text(a, 200) for a in result.get("action_items") or [] if isinstance(a, str)][:3],
        "confidence": result.get("confidence"),
        "generated_by": result.get("generated_by") or "unavailable",
    }


# ---------------------------------------------------------------------------
# Writing evidence
# ---------------------------------------------------------------------------

def _clean_date(value: str | None, today: date) -> str:
    try:
        day = date.fromisoformat((value or "")[:10])
    except ValueError:
        return today.isoformat()
    return min(day, today).isoformat() if day >= today - timedelta(days=730) else today.isoformat()


def import_message(user_id: str, *, source: str, subject: str, body: str, sender: str | None,
                   sent_on: str | None, by: str, today: date) -> str:
    """A tagged email or Teams conversation -> pending, AI-classified suggestion for the manager."""
    if source not in ("outlook", "teams", "email_file", "integration"):
        raise EvidenceError("Unknown source.")
    text = redact(clean_multiline_text(body, 20000))
    if len(text) < 20:
        raise EvidenceError("There isn't enough text in that message to tag.")
    content_hash = _content_hash(user_id, text)
    suggestion = classify(text, role_skills(user_id), source)
    title = sanitise_text(redact(subject), 140) or (
        "Teams conversation" if source == "teams" else "Email") + f" · {_clean_date(sent_on, today)}"
    evidence_id = store.add_evidence(
        user_id=user_id, kind=suggestion["kind"], source=source, title=title,
        summary=suggestion["summary"] or sanitise_text(text, 300), occurred_on=_clean_date(sent_on, today),
        sentiment=suggestion["sentiment"], created_by=by, skills=suggestion["skills"], status="pending", shared=True,
        origin_sender=sanitise_text(redact(sender or ""), 80) or None, excerpt=text[:EXCERPT_CHARS],
        content_hash=content_hash, ai=suggestion, action_items=suggestion["action_items"])
    if not evidence_id:
        raise EvidenceError("This message is already tagged to this employee.")
    return evidence_id


def add_manual(user_id: str, *, kind: str, title: str, summary: str, occurred_on: str, skills: list[str],
               weight: int, shared: bool, action_items: list[str], create_actions: bool, by: str, today: date) -> str:
    if kind not in KIND_SENTIMENT:
        raise EvidenceError("Choose what kind of evidence this is.")
    title, summary = sanitise_text(title, 140), clean_multiline_text(summary, 2000)
    if len(title) < 3 or len(summary) < 15:
        raise EvidenceError("Add a short title and the facts: what happened, when, and the impact.")
    allowed = {s.lower(): s for s in role_skills(user_id)}
    items = [sanitise_text(a, 200) for a in action_items if sanitise_text(a, 200)][:6]
    evidence_id = store.add_evidence(
        user_id=user_id, kind=kind, source="manual", title=title, summary=summary,
        occurred_on=_clean_date(occurred_on, today), sentiment=KIND_SENTIMENT[kind], created_by=by,
        skills=[allowed[s.lower()] for s in skills if s.lower() in allowed][:4], weight=weight, status="confirmed",
        shared=shared, action_items=items)
    if create_actions and items:
        development.add_from_one_on_one(user_id, items, evidence_id, by, today)
    if evidence_id:
        evaluate(evidence_id, user_id, kind=kind, title=title, summary=summary)
    _after_confirm(user_id, kind, title, shared)
    return evidence_id or ""


def evaluate(evidence_id: str, user_id: str, *, kind: str, title: str, summary: str) -> dict | None:
    """Agent 7's second reading of a note the manager wrote: the skills it shows, a factual
    one-line context and a tone check. Stored beside the record (never over it), shown to the
    manager and leader, and fed to the performance summary (Agent 9). Nothing is stored when
    the pipeline API is unavailable -- the note itself is already saved."""
    result = classify(f"{title}\n{summary}", role_skills(user_id), "manual")
    if result["generated_by"] == "unavailable":
        return None
    ai = {**result, "mode": "evaluation"}
    if result["kind"] != kind and result["sentiment"] != KIND_SENTIMENT[kind]:
        ai["tone_check"] = (f"Agent 7 reads this as {KIND_LABELS[result['kind']].lower()} ({result['sentiment']}); "
                            f"it was logged as {KIND_LABELS[kind].lower()}. Check the wording or the kind.")
    store.set_evidence_ai(evidence_id, ai)
    return ai


def _after_confirm(user_id: str, kind: str, title: str, shared: bool) -> None:
    if shared:
        label = KIND_LABELS.get(kind, "Evidence")
        engagement.notify([user_id], "evidence", f"{label} added to your record: {title}",
                          "You can read it and add your comment.", "/employee/performance#evidence")


def confirm(evidence: dict, by: str, *, kind: str, sentiment: str, title: str, summary: str, skills: list[str],
            shared: bool, weight: int, create_actions: bool, today: date) -> bool:
    if kind not in KIND_SENTIMENT or sentiment not in ("positive", "neutral", "negative"):
        raise EvidenceError("Choose the kind and tone.")
    allowed = {s.lower(): s for s in role_skills(evidence["user_id"])}
    ok = store.decide_evidence(
        evidence["evidence_id"], "confirmed", by, kind=kind, sentiment=sentiment,
        title=sanitise_text(title, 140) or evidence["title"], summary=clean_multiline_text(summary, 2000) or evidence["summary"],
        skills_json=json.dumps([allowed[s.lower()] for s in skills if s.lower() in allowed][:4]),
        shared=1 if shared else 0, weight=max(1, min(3, weight)))
    if ok:
        if create_actions and evidence["action_items"]:
            development.add_from_one_on_one(evidence["user_id"], evidence["action_items"], evidence["evidence_id"], by, today)
        _after_confirm(evidence["user_id"], kind, title or evidence["title"], shared)
    return ok


def ingest(payload: dict, today: date) -> str:
    """Power Automate / Graph flow: the tagger must be the employee's manager. Raises
    LookupError (unknown people) or PermissionError (not their manager)."""
    employee = store.user_by_email(str(payload.get("employee_email") or "")[:254])
    tagger = store.user_by_email(str(payload.get("tagged_by_email") or "")[:254])
    if not employee or not tagger or employee["role"] != "employee":
        raise LookupError("Unknown employee or tagger.")
    if employee.get("manager_id") != tagger["user_id"]:
        raise PermissionError("Only the employee's manager can tag evidence to them.")
    source = payload.get("source") if payload.get("source") in ("outlook", "teams") else "integration"
    evidence_id = import_message(employee["user_id"], source=source, subject=str(payload.get("subject") or ""),
                                 body=str(payload.get("body") or ""), sender=payload.get("sender"),
                                 sent_on=payload.get("sent_at"), by=tagger["user_id"], today=today)
    engagement.notify([tagger["user_id"]], "evidence_pending", f"Tagged message for {employee['full_name']} to confirm",
                      "Agent 7 suggested how to record it.", f"/people/{employee['user_id']}#evidence")
    return evidence_id


# ---------------------------------------------------------------------------
# Reading evidence
# ---------------------------------------------------------------------------

def timeline(user_id: str, *, employee_view: bool) -> list[dict]:
    """Confirmed evidence, newest first. The employee sees only what was shared with them."""
    items = store.list_evidence(user_id, shared_only=employee_view)
    for item in items:
        item["kind_label"] = KIND_LABELS.get(item["kind"], item["kind"])
        item["source_label"] = SOURCE_LABELS.get(item["source"], item["source"])
    return items


def pending(user_id: str) -> list[dict]:
    items = store.list_evidence(user_id, statuses=("pending",))
    for item in items:
        item["kind_label"] = KIND_LABELS.get(item["kind"], item["kind"])
        item["source_label"] = SOURCE_LABELS.get(item["source"], item["source"])
    return items


def counts(items: list[dict], today: date, days: int = 90) -> dict:
    cutoff = (today - timedelta(days=days)).isoformat()
    recent = [i for i in items if i["occurred_on"] >= cutoff]
    return {"positive": sum(1 for i in recent if i["sentiment"] == "positive"),
            "negative": sum(1 for i in recent if i["sentiment"] == "negative"),
            "one_on_one": sum(1 for i in recent if i["kind"] == "one_on_one"), "total": len(recent), "days": days}
