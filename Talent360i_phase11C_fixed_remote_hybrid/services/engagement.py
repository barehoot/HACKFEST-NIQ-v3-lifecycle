"""Reaching people: in-app notifications (the top-bar bell), the email / Teams
outbox, weekly pulse check-ins and peer recognition.

Notifications are always stored in-app. When TALENT360_NOTIFY_CHANNELS lists
"email" and/or "teams", each one is also appended to logs/outbox.jsonl; in
production the lifecycle worker drains that outbox through Microsoft Graph
(sendMail, Teams activity feed). The outbox keeps the user id, never an email
address, so it can be shipped to a log pipeline without PII.

Pulse check-ins are a wellbeing signal only: the status engine never lets
them move anyone's performance score (services/performance.py).
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date, timedelta
from pathlib import Path

from domain.governance import sanitise_text
from services import db, lifecycle_store as store

_OUTBOX_LOCK = threading.Lock()
EXTERNAL_CHANNELS = ("email", "teams")
WELLBEING_MOOD_MAX = 2.0       # average mood at or below -> check-in suggested
WELLBEING_WORKLOAD_MIN = 4.5   # average workload at or above -> check-in suggested
KUDOS_MIN_CHARS = 15


def _channels() -> list[str]:
    configured = [c.strip().lower() for c in os.getenv("TALENT360_NOTIFY_CHANNELS", "in_app").split(",")]
    return ["in_app"] + [c for c in EXTERNAL_CHANNELS if c in configured]


def _outbox_path() -> Path:
    return Path(os.getenv("TALENT360_LOG_DIR", Path(__file__).resolve().parents[2] / "logs")) / "outbox.jsonl"


def _queue_external(user_id: str, channels: list[str], kind: str, title: str, link: str | None) -> None:
    record = {"user_id": user_id, "channels": channels, "kind": kind, "title": title, "link": link,
              "queued_at": store._now(), "status": "queued"}
    try:
        with _OUTBOX_LOCK:
            path = _outbox_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
    except OSError:
        pass  # the in-app copy is already stored; never fail the request over the outbox


def notify(user_ids, kind: str, title: str, body: str = "", link: str | None = None) -> int:
    channels = _channels()
    sent = 0
    for user_id in dict.fromkeys(u for u in user_ids if u):
        store.add_notification(user_id=user_id, kind=kind, title=sanitise_text(title, 160), body=sanitise_text(body, 500),
                               link=link, channels=",".join(channels))
        external = [c for c in channels if c != "in_app"]
        if external:
            _queue_external(user_id, external, kind, sanitise_text(title, 160), link)
        sent += 1
    return sent


def leaders_for(user_id: str) -> list[str]:
    """The leader(s) responsible for this employee: their department's leader, else every leader."""
    profile = store.get_profile(user_id) or {}
    department = store.get_department(profile["dept_id"]) if profile.get("dept_id") else None
    if department and department.get("leader_id"):
        return [department["leader_id"]]
    return store.user_ids_with_role("leader")


def manager_of(user_id: str) -> str | None:
    return (db.get_user(user_id) or {}).get("manager_id")


# ---------------------------------------------------------------------------
# Pulse check-ins
# ---------------------------------------------------------------------------

def week_key(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def record_pulse(user_id: str, mood: int, workload: int, comment: str, shared: bool, today: date) -> None:
    if not (1 <= mood <= 5 and 1 <= workload <= 5):
        raise ValueError("Choose a value from 1 to 5 for both questions.")
    store.upsert_pulse(user_id=user_id, week=week_key(today), mood=mood, workload=workload,
                       comment=sanitise_text(comment, 500), shared=shared)


def wellbeing_flag(pulses: list[dict]) -> bool:
    """Last three check-ins (newest first) average low mood or very high workload."""
    recent = pulses[:3]
    if len(recent) < 2:
        return False
    mood = sum(p["mood"] for p in recent) / len(recent)
    workload = sum(p["workload"] for p in recent) / len(recent)
    return mood <= WELLBEING_MOOD_MAX or workload >= WELLBEING_WORKLOAD_MIN


def my_pulse(user_id: str, today: date) -> dict:
    pulses = store.pulses_for_users([user_id], week_key(today - timedelta(weeks=8))).get(user_id, [])
    return {"this_week": next((p for p in pulses if p["week"] == week_key(today)), None), "history": pulses}


def team_pulse(user_ids: list[str], today: date) -> dict:
    """Last four weeks for a manager: averages, response rate and who may need a check-in.
    Comments appear only when the employee chose to share them."""
    by_user = store.pulses_for_users(user_ids, week_key(today - timedelta(weeks=4)))
    latest = [p[0] for p in by_user.values() if p]
    responded = sum(1 for p in latest if p["week"] == week_key(today))
    return {
        "responses": len(latest), "this_week": responded, "team": len(user_ids),
        "response_pct": round(100 * responded / len(user_ids)) if user_ids else 0,
        "avg_mood": round(sum(p["mood"] for p in latest) / len(latest), 1) if latest else None,
        "avg_workload": round(sum(p["workload"] for p in latest) / len(latest), 1) if latest else None,
        "check_in": [uid for uid, pulses in by_user.items() if wellbeing_flag(pulses)],
        "comments": [{"user_id": uid, "week": p["week"], "comment": p["comment"]}
                     for uid, pulses in by_user.items() for p in pulses[:1] if p["shared"] and p["comment"]],
    }


# ---------------------------------------------------------------------------
# Peer recognition
# ---------------------------------------------------------------------------

def send_kudos(*, sender: dict, recipient_id: str, message: str, skill: str | None, today: date) -> str:
    """A colleague's thank-you: pending appreciation evidence the recipient's manager confirms,
    so peers can't move anyone's status on their own."""
    recipient = db.get_user(recipient_id)
    if not recipient or recipient["role"] != "employee" or recipient_id == sender["id"]:
        raise ValueError("Choose a colleague to recognise.")
    text = sanitise_text(message, 600)
    if len(text) < KUDOS_MIN_CHARS:
        raise ValueError("Say what they did, in a sentence or two.")
    evidence_id = store.add_evidence(
        user_id=recipient_id, kind="appreciation", source="peer", title=f"Recognition from {sender['name']}",
        summary=text, occurred_on=today.isoformat(), sentiment="positive", created_by=sender["id"],
        skills=[skill] if skill else [], status="pending", shared=True, origin_sender=sender["name"])
    notify([recipient_id], "kudos", f"{sender['name']} recognised your work", text, "/employee/performance")
    if recipient.get("manager_id"):
        notify([recipient["manager_id"]], "evidence_pending", f"Peer recognition for {recipient['full_name']} to confirm",
               text, f"/people/{recipient_id}#evidence")
    return evidence_id or ""
