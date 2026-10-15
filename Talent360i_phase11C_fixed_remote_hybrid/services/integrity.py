"""
Assessment integrity: what the proctored browser reports during an attempt, the
strike rules, the risk it adds up to, and the penalty that follows.

A web page cannot stop someone switching tabs, closing the page or reading from a
phone. It can state the rules, lock the assessment into full screen, block copy and
paste, notice every time the candidate leaves, check for a second display, and
record when. This module turns those reports (static/js/proctor.js) into:

  strike  leaving the page (another tab, window or app) for GRACE_SECONDS or more,
          exiting full screen, closing / reloading / navigating away, or a second
          display. A warning each time; at MAX_STRIKES the attempt is submitted
          automatically and the manager is told.
  minor   blocked copy, paste, drag-and-drop, right-click, screenshot key or
          developer-tools shortcut, and focus changes shorter than GRACE_SECONDS
          (a notification, a misclick). Recorded, never a strike.
  info    rules accepted, page loads, whether the browser could check for displays.

risk  high    strike limit reached, a second display, or HIGH_AWAY_SECONDS away in total
      medium  any strike, MEDIUM_AWAY_SECONDS away, 3+ blocked actions, text inserted
              without typing, or 2+ answers typed like a transcription (typing_signals)
      low     anything less

penalty (progression.assessment_outcome / decide and question_bank apply it, visibly):
      medium  MEDIUM_MCQ_PENALTY points off each skill's MCQ score when judging the
              recommendation (the raw score is kept and shown)
      high    the recommendation becomes "send back to retake"; certifying needs a
              written override; the retake opens COOL_OFF_DAYS after the decision; a
              second high-risk attempt within REPEAT_WINDOW_DAYS notifies HR

The manager sees every event with its time and still makes the decision.
"""
import os
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from services import db, engagement, lifecycle_store as store

MAX_STRIKES = int(os.getenv("TALENT360_INTEGRITY_MAX_STRIKES", "3"))
GRACE_SECONDS = float(os.getenv("TALENT360_INTEGRITY_GRACE_SECONDS", "2"))
COOL_OFF_DAYS = int(os.getenv("TALENT360_INTEGRITY_COOL_OFF_DAYS", "7"))
REQUIRE_FULLSCREEN = os.getenv("TALENT360_INTEGRITY_FULLSCREEN", "true").strip().lower() != "false"
REPEAT_WINDOW_DAYS = 180
MEDIUM_MCQ_PENALTY = 10
HIGH_AWAY_SECONDS = 60
MEDIUM_AWAY_SECONDS = 15
EPISODE_SECONDS = 3  # strikes this close are one episode: a tab switch also drops full screen
MAX_EVENTS_PER_ATTEMPT = 400
MAX_EVENTS_PER_REQUEST = 20

STAGE_STATUS = {"mcq": "ready_for_assessment", "simulation": "mcq_submitted"}
KINDS = {  # what the browser may report -> severity before the rules below
    "rules_accepted": "info", "page_loaded": "info", "screen_check_unavailable": "info", "stage_started": "info",
    "focus_lost": "strike", "fullscreen_exit": "strike", "left_page": "strike", "reopened": "strike",
    "multi_screen": "strike",
    "paste_blocked": "minor", "copy_blocked": "minor", "drop_blocked": "minor", "context_menu": "minor",
    "screenshot_key": "minor", "devtools_shortcut": "minor",
}
BLOCKED = ("paste_blocked", "copy_blocked", "drop_blocked", "context_menu", "screenshot_key", "devtools_shortcut")
LABELS = {
    "rules_accepted": "Accepted the rules and started", "page_loaded": "Opened the assessment page",
    "stage_started": "Moved on to the next part of the assessment",
    "screen_check_unavailable": "Browser can't check for a second display",
    "focus_lost": "Left the assessment (another tab, window or app)", "fullscreen_exit": "Exited full screen",
    "left_page": "Closed, reloaded or navigated away from the page", "reopened": "Reopened the page after leaving it",
    "multi_screen": "Second display detected", "paste_blocked": "Paste blocked", "copy_blocked": "Copy blocked",
    "drop_blocked": "Drag-and-drop blocked", "context_menu": "Right-click blocked", "screenshot_key": "Screenshot key pressed",
    "devtools_shortcut": "Developer-tools shortcut blocked",
    "strike_limit": "Strike limit reached: attempt submitted automatically",
}
PENALTIES = {
    "low": {"label": "No penalty", "mcq_points": 0},
    "medium": {"label": f"{MEDIUM_MCQ_PENALTY} points off each skill's MCQ score when judging the recommendation",
               "mcq_points": MEDIUM_MCQ_PENALTY},
    "high": {"label": (f"Recommendation: send back to retake · certifying needs a written override · the retake opens "
                       f"{COOL_OFF_DAYS} days after the decision · a repeat within {REPEAT_WINDOW_DAYS} days alerts HR"),
             "mcq_points": None},
}
OVERRIDE_MIN_CHARS = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _seconds(value) -> float | None:
    try:
        return max(0.0, min(float(value), 86_400.0))
    except (TypeError, ValueError):
        return None


def _detail(value) -> dict:
    """Only small scalar facts from the browser (which way focus went, display flag); nothing free-form."""
    if not isinstance(value, dict):
        return {}
    out = {}
    for key in ("via", "extended", "fullscreen_lost", "visible"):
        v = value.get(key)
        if isinstance(v, bool) or v is None:
            out[key] = v
        elif isinstance(v, str) and v in ("tab", "window", "app"):
            out[key] = v
    return {k: v for k, v in out.items() if v is not None}


def _left_since_last_load(events: list[dict]) -> bool:
    for e in reversed(events):
        if e["kind"] == "left_page":
            return True
        if e["kind"] == "page_loaded":
            return False
    return False


# ---------------------------------------------------------------------------
# Typing signals (simulation answers)
# ---------------------------------------------------------------------------

def typing_signals(cycle: dict | None) -> dict:
    """How each simulation answer reached the box, from what the browser measured:
    characters typed, characters inserted some other way (drop, autocorrect, an
    extension), corrections and long pauses. Two patterns are flagged:
      inserted     much of the answer arrived without being typed or was injected;
      transcribed  a long answer typed steadily with almost no corrections and no
                   pause to think, which is what copying from another screen looks like.
    Neither is proof; both are shown to the manager as signals."""
    saved = (cycle or {}).get("case_study_answers") or {}
    turns = [t for ts in (saved.get("transcripts") or {}).values() for t in ts
             if t.get("role") == "candidate"] if saved.get("mode") == "simulation" else []
    measured = inserted = transcribed = 0
    for turn in turns:
        meta = turn.get("meta") or {}
        if meta.get("typed_chars") is None:
            continue  # answered before typing was measured
        measured += 1
        chars = len(turn.get("text") or "")
        typed, other = meta.get("typed_chars") or 0, (meta.get("inserted_chars") or 0) + (meta.get("paste_chars") or 0)
        if chars >= 40 and (other >= 40 or chars > typed + other + 20):
            inserted += 1
        elif chars >= 250 and (meta.get("corrections") or 0) <= chars / 200 and not meta.get("long_pauses"):
            transcribed += 1
    return {"measured": measured, "inserted": inserted, "transcribed": transcribed}


# ---------------------------------------------------------------------------
# Summary, risk and penalty
# ---------------------------------------------------------------------------

def summarize(events: list[dict], typing: dict | None = None) -> dict:
    counts = Counter(e["kind"] for e in events)
    strikes = sum(1 for e in events if e["severity"] == "strike")
    away = sum(e["seconds"] or 0 for e in events if e["kind"] in ("focus_lost", "left_page"))
    blocked = sum(counts[k] for k in BLOCKED)
    limit = counts["strike_limit"] > 0
    multi = counts["multi_screen"] > 0
    accepted = [e for e in events if e["kind"] == "rules_accepted"]
    screen = None
    if accepted:
        extended = accepted[-1]["detail"].get("extended")
        screen = "extended" if extended is True else "single" if extended is False else "unavailable"
    if multi:
        screen = "extended"
    typing = typing or {"measured": 0, "inserted": 0, "transcribed": 0}

    reasons = []
    if limit:
        reasons.append(f"{strikes} of {MAX_STRIKES} strikes: the attempt was submitted automatically")
    elif strikes:
        reasons.append(f"{strikes} strike{'s' if strikes != 1 else ''} of {MAX_STRIKES}")
    if multi:
        reasons.append("a second display was detected")
    if away >= MEDIUM_AWAY_SECONDS:
        reasons.append(f"away from the assessment for {round(away)} s in total")
    if blocked >= 3:
        reasons.append(f"{blocked} blocked copy, paste or screenshot attempts")
    if typing["inserted"]:
        reasons.append(f"{typing['inserted']} answer(s) contained text that wasn't typed")
    if typing["transcribed"] >= 2:
        reasons.append(f"{typing['transcribed']} long answers typed steadily with no corrections or pauses (possible transcription)")

    if limit or multi or away >= HIGH_AWAY_SECONDS:
        risk = "high"
    elif strikes or away >= MEDIUM_AWAY_SECONDS or blocked >= 3 or typing["inserted"] or typing["transcribed"] >= 2:
        risk = "medium"
    else:
        risk = "low"
    return {
        "risk": risk, "strikes": strikes, "max_strikes": MAX_STRIKES, "away_seconds": round(away),
        "screen": screen, "accepted": bool(accepted), "proctored": bool(events),
        "locked_stage": next((e["stage"] for e in events if e["kind"] == "strike_limit"), None),
        "auto_submitted": limit, "blocked_actions": blocked, "counts": dict(counts), "typing": typing,
        "reasons": reasons, "penalty": PENALTIES[risk]["label"], "mcq_points": PENALTIES[risk]["mcq_points"],
    }


def refresh(cycle: dict) -> dict:
    """Recomputes and stores the attempt's summary (events plus typing signals)."""
    summary = summarize(store.integrity_events(cycle["cycle_id"]),
                        typing_signals(db.get_cycle(cycle["cycle_id"]) or cycle))
    store.save_integrity_summary(cycle["cycle_id"], cycle["user_id"], summary)
    return summary


def summary(cycle_id: str) -> dict | None:
    """The stored summary, or None for an attempt that was never proctored."""
    return store.integrity_summaries([cycle_id]).get(cycle_id)


def summaries(cycle_ids) -> dict[str, dict]:
    return store.integrity_summaries(list(cycle_ids))


def timeline(cycle_id: str) -> list[dict]:
    return [{**e, "label": LABELS.get(e["kind"], e["kind"].replace("_", " "))} for e in store.integrity_events(cycle_id)]


def client_view(cycle_id: str, stage: str | None = None) -> dict:
    """What the proctored page needs to start: was the pledge taken, strikes so far, the limit,
    and whether this part of the attempt was already under way (so opening it again is a return)."""
    s = summary(cycle_id) or summarize([])
    seen = bool(stage) and any(e["stage"] == stage for e in store.integrity_events(cycle_id))
    return {"accepted": s["accepted"], "strikes": s["strikes"], "max_strikes": MAX_STRIKES, "stage_seen": seen,
            "locked": bool(s["locked_stage"]), "fullscreen": REQUIRE_FULLSCREEN, "grace": GRACE_SECONDS,
            "cool_off_days": COOL_OFF_DAYS, "penalties": PENALTIES, "medium_points": MEDIUM_MCQ_PENALTY}


def locked(cycle_id: str) -> bool:
    s = summary(cycle_id)
    return bool(s and s["locked_stage"])


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def record(cycle: dict, stage: str, raw_events: list) -> dict:
    """Stores what the browser reported for this attempt and applies the strike rules.
    Returns the strikes so far, the latest strike (for the warning) and whether the
    limit was reached, in which case the caller ends the attempt."""
    existing = store.integrity_events(cycle["cycle_id"])
    before = summarize(existing)
    if before["locked_stage"]:
        return {"strikes": before["strikes"], "max_strikes": MAX_STRIKES, "warning": None, "limit": True}
    now = _now()
    stamp = now.isoformat(timespec="milliseconds")
    last_strike = max((e["created_at"] for e in existing if e["severity"] == "strike"), default=None)
    budget = MAX_EVENTS_PER_ATTEMPT - len(existing)
    rows, warning = [], None
    for raw in (raw_events or [])[:MAX_EVENTS_PER_REQUEST]:
        kind = raw.get("kind") if isinstance(raw, dict) else None
        if kind not in KINDS:
            continue
        severity, seconds, detail = KINDS[kind], _seconds(raw.get("seconds")), _detail(raw.get("detail"))
        if kind == "focus_lost" and (seconds or 0) < GRACE_SECONDS:
            severity = "minor"  # a notification or a misclick, not leaving
        if kind == "reopened" and _left_since_last_load(existing + rows):
            severity = "info"  # closing the page was the strike
        if kind == "reopened" and not any(e["stage"] == stage for e in existing + rows):
            severity, kind = "info", "stage_started"  # first visit to this part (MCQ done -> simulation): not a return
        if severity == "strike" and last_strike and (now - _parse(last_strike)).total_seconds() < EPISODE_SECONDS:
            severity, detail = "minor", {**detail, "same_episode": True}
        if budget <= 0 and severity != "strike":
            continue
        row = {"cycle_id": cycle["cycle_id"], "user_id": cycle["user_id"], "stage": stage, "kind": kind,
               "severity": severity, "seconds": seconds, "detail": detail, "created_at": stamp}
        rows.append(row)
        budget -= 1
        if severity == "strike":
            last_strike = stamp
            warning = {"kind": kind, "label": LABELS[kind], "seconds": seconds}
    strikes = before["strikes"] + sum(1 for r in rows if r["severity"] == "strike")
    limit = strikes >= MAX_STRIKES
    if limit:
        rows.append({"cycle_id": cycle["cycle_id"], "user_id": cycle["user_id"], "stage": stage, "kind": "strike_limit",
                     "severity": "system", "seconds": None, "detail": {"strikes": strikes}, "created_at": stamp})
    store.add_integrity_events(rows)
    refresh(cycle)
    if limit:
        _tell_manager(cycle, strikes)
    return {"strikes": strikes, "max_strikes": MAX_STRIKES, "warning": warning, "limit": limit}


def _tell_manager(cycle: dict, strikes: int) -> None:
    user = db.get_user(cycle["user_id"]) or {}
    if user.get("manager_id"):
        engagement.notify([user["manager_id"]], "integrity",
                          f"{user.get('full_name') or cycle['user_id']}'s assessment was submitted automatically",
                          f"{strikes} integrity strikes (leaving the page, exiting full screen or a second display). "
                          "The timeline is on the review page; you decide what happens next.",
                          f"/manager/validations/{cycle['cycle_id']}")


# ---------------------------------------------------------------------------
# Penalties that outlive the decision
# ---------------------------------------------------------------------------

def cool_off_until(user_id: str, today: date | None = None) -> date | None:
    """The day a retake opens when the latest decided attempt was sent back for a
    high-risk integrity record; None when nothing is holding it."""
    today = today or date.today()
    decided = [c for c in db.list_cycles_for_user(user_id) if c["status"] in ("manager_approved", "manager_rejected")]
    if not decided or decided[0].get("manager_decision") != "sent_back":
        return None
    s = summary(decided[0]["cycle_id"])
    if not s or s["risk"] != "high":
        return None
    opens = date.fromisoformat((decided[0]["updated_at"] or today.isoformat())[:10]) + timedelta(days=COOL_OFF_DAYS)
    return opens if opens > today else None


def report_repeat(cycle: dict, decided_by: str) -> bool:
    """After a decision on a high-risk attempt: a second one within the window alerts HR."""
    since = (_now() - timedelta(days=REPEAT_WINDOW_DAYS)).isoformat(timespec="seconds")
    others = [c for c in store.high_risk_attempts(cycle["user_id"], since) if c != cycle["cycle_id"]]
    if not others:
        return False
    user = db.get_user(cycle["user_id"]) or {}
    engagement.notify(store.user_ids_with_role("hr"), "integrity",
                      f"Repeated assessment-integrity breach: {user.get('full_name') or cycle['user_id']}",
                      f"{len(others) + 1} high-risk attempts in {REPEAT_WINDOW_DAYS} days. Decided by the manager; "
                      "consider a conversation about assessment conduct.", f"/people/{cycle['user_id']}")
    return True
