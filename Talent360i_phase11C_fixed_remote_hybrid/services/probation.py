"""Probation for new joiners: six months, tracked automatically, decided by people.

  - A probation record starts from the employee's hire date (employee_profiles,
    Workday-synced in production): ensure_for_new_joiners() creates one for anyone
    hired within the last six months, with three check-ins -- Month 1, Month 3 and a
    Month 5 pre-decision review.
  - The manager completes each check-in (on track / at risk / off track + a note the
    employee can read), alongside the same status engine every employee has.
  - From 30 days before the end date the manager is asked for a recommendation:
    confirm, extend by 90 days, or do not confirm. "Do not confirm" needs at least two
    documented check-ins and a written rationale -- a negative outcome must rest on
    recorded facts, not a last-minute view.
  - The business leader decides: approve the recommendation, or return it to the
    manager with a note. Talent 360i records the outcome for HR; it never changes an
    employment record itself.
"""
from __future__ import annotations

from datetime import date, timedelta

from domain.governance import sanitise_text
from services import db, engagement, lifecycle_store as store

PROBATION_DAYS = 182
EXTENSION_DAYS = 90
CHECKPOINTS = (("Month 1 check-in", 30), ("Month 3 review", 90), ("Month 5 pre-decision review", 150))
RECOMMENDATIONS = {"confirm": "Confirm employment", "extend": "Extend probation by 90 days",
                   "not_confirm": "Do not confirm"}
RATINGS = {"on_track": "On track", "at_risk": "At risk", "off_track": "Off track"}
STATUS_LABELS = {"active": "In probation", "pending_leader": "Decision with leader", "confirmed": "Confirmed",
                 "not_confirmed": "Not confirmed"}


class ProbationError(ValueError):
    pass


def start(user_id: str, start_date: date) -> str | None:
    end = start_date + timedelta(days=PROBATION_DAYS)
    return store.create_probation(user_id, start_date.isoformat(), end.isoformat(),
                                  [(label, (start_date + timedelta(days=d)).isoformat()) for label, d in CHECKPOINTS])


def ensure_for_new_joiners(today: date) -> int:
    """Starts probation for every employee hired in the last six months who doesn't have one yet."""
    cutoff = (today - timedelta(days=PROBATION_DAYS)).isoformat()
    created = 0
    for user_id, hire_date in store.new_joiners_without_probation(cutoff, today.isoformat()):
        if start(user_id, date.fromisoformat(hire_date)):
                created += 1
                manager = engagement.manager_of(user_id)
                engagement.notify([manager], "probation", "New joiner on probation",
                                  "Three check-ins are scheduled; the first is due 30 days after the start date.",
                                  f"/people/{user_id}#probation")
    return created


def view(user_id: str, today: date) -> dict | None:
    record = store.probation_for_user(user_id)
    if not record:
        return None
    return _view_from(record, store.checkpoints(record["probation_id"]), today)


def _view_from(record: dict, checkpoints: list[dict], today: date) -> dict:
    start_day, end_day = date.fromisoformat(record["start_date"]), date.fromisoformat(record["end_date"])
    span = max((end_day - start_day).days, 1)
    for k in checkpoints:
        k["state"] = ("done" if k.get("completed_at") else "overdue" if k["due_date"] < today.isoformat() else
                      "due" if k["due_date"] <= (today + timedelta(days=7)).isoformat() else "upcoming")
        k["rating_label"] = RATINGS.get(k.get("rating"), "")
        k["pos_pct"] = max(2, min(98, round(100 * (date.fromisoformat(k["due_date"]) - start_day).days / span)))
        k["short_label"] = " ".join(k["label"].split()[:2])
    days_left = (end_day - today).days
    done = [k for k in checkpoints if k.get("completed_at")]
    return {
        **record, "checkpoints": checkpoints, "days_left": days_left,
        "progress_pct": max(0, min(100, round(100 * (today - start_day).days / span))),
        "status_label": STATUS_LABELS.get(record["status"], record["status"]),
        "recommendation_label": RECOMMENDATIONS.get(record.get("recommendation") or "", ""),
        "decision_due": record["status"] == "active" and days_left <= 30,
        "can_recommend": record["status"] == "active" and len(done) >= 1,
        "can_not_confirm": len(done) >= 2,
        "latest_rating": RATINGS.get(done[-1]["rating"]) if done else None,
        "open": record["status"] in ("active", "pending_leader"),
    }


def complete_checkpoint(user_id: str, checkpoint_id: str, rating: str, note: str, by: str) -> None:
    record = store.probation_for_user(user_id)
    if not record or record["status"] != "active":
        raise ProbationError("There is no active probation for this employee.")
    if rating not in RATINGS:
        raise ProbationError("Choose on track, at risk or off track.")
    note = sanitise_text(note, 1500)
    if len(note) < 20:
        raise ProbationError("Write what you observed (at least a sentence) -- the employee will read it.")
    if not store.complete_checkpoint(checkpoint_id, record["probation_id"], rating, note, by):
        raise ProbationError("That check-in doesn't belong to this probation.")
    engagement.notify([user_id], "probation", "Your probation check-in was recorded",
                      f"Rated {RATINGS[rating].lower()}. Read the note on your performance page.", "/employee/performance#probation")


def recommend(user_id: str, recommendation: str, note: str, by: str, today: date) -> None:
    record = view(user_id, today)
    if not record or record["status"] != "active":
        raise ProbationError("There is no active probation for this employee.")
    if recommendation not in RECOMMENDATIONS:
        raise ProbationError("Choose confirm, extend or do not confirm.")
    if not record["can_recommend"]:
        raise ProbationError("Complete at least one check-in before recommending an outcome.")
    note = sanitise_text(note, 2000)
    if recommendation == "not_confirm":
        if not record["can_not_confirm"]:
            raise ProbationError("Not confirming needs at least two documented check-ins first.")
        if len(note) < 40:
            raise ProbationError("Explain the decision with the facts behind it (a few sentences).")
    elif len(note) < 20:
        raise ProbationError("Add a short rationale for the leader.")
    if not store.recommend_probation(record["probation_id"], recommendation, note, by):
        raise ProbationError("This probation is no longer waiting for a recommendation.")
    name = (db.get_user(user_id) or {}).get("full_name", user_id)
    engagement.notify(engagement.leaders_for(user_id), "probation_decision",
                      f"Probation decision needed: {name}", f"Manager recommends: {RECOMMENDATIONS[recommendation]}.",
                      "/leader/probation")


def decide(probation_id: str, decision: str, note: str, leader_id: str) -> dict:
    record = store.get_probation(probation_id)
    if not record or record["status"] != "pending_leader":
        raise ProbationError("This probation is not waiting for a decision.")
    note = sanitise_text(note, 2000)
    if decision == "return":
        if len(note) < 10:
            raise ProbationError("Tell the manager what to look at again.")
        store.decide_probation(probation_id, status="active", leader_id=leader_id, note=note, event="returned")
        engagement.notify([engagement.manager_of(record["user_id"])], "probation", "Probation recommendation returned",
                          note, f"/people/{record['user_id']}#probation")
        return {"outcome": "returned"}
    if decision != "approve":
        raise ProbationError("Approve the recommendation or return it to the manager.")
    recommendation = record["recommendation"]
    if recommendation == "extend":
        new_end = date.fromisoformat(record["end_date"]) + timedelta(days=EXTENSION_DAYS)
        store.decide_probation(probation_id, status="active", leader_id=leader_id, note=note,
                               end_date=new_end.isoformat(), event="extended")
        store.add_checkpoint(probation_id, "Extension review", (new_end - timedelta(days=15)).isoformat())
        outcome = "extended"
    else:
        status = "confirmed" if recommendation == "confirm" else "not_confirmed"
        store.decide_probation(probation_id, status=status, leader_id=leader_id, note=note, event=status)
        outcome = status
    manager = engagement.manager_of(record["user_id"])
    engagement.notify([manager], "probation", f"Probation decision recorded: {outcome.replace('_', ' ')}",
                      note or "Approved as recommended.", f"/people/{record['user_id']}#probation")
    if outcome in ("confirmed", "extended"):
        engagement.notify([record["user_id"]], "probation",
                          "Your probation is confirmed" if outcome == "confirmed" else "Your probation was extended by 90 days",
                          "Your manager will talk you through it.", "/employee/performance#probation")
    return {"outcome": outcome}


def summary(user_ids: set[str] | None, today: date) -> dict:
    """Headline numbers plus the recommendations waiting for a leader (the overview's needs)."""
    counts = store.probation_counts(None if user_ids is None else sorted(user_ids), today.isoformat(),
                                    (today + timedelta(days=30)).isoformat())
    waiting = [r for r in store.list_probations(statuses=("pending_leader",))
               if user_ids is None or r["user_id"] in user_ids]
    for r in waiting:
        r["recommendation_label"] = RECOMMENDATIONS.get(r.get("recommendation") or "", "")
    return {**counts, "rows": waiting}


def pipeline(user_ids: set[str] | None, today: date) -> dict:
    """Probation tracker rows (all, or one scope), with what needs attention first."""
    records = [r for r in store.list_probations() if user_ids is None or r["user_id"] in user_ids]
    checkpoints = store.checkpoints_for([r["probation_id"] for r in records])  # one query for the whole tracker
    rows = []
    for record in records:
        v = _view_from(record, checkpoints.get(record["probation_id"], []), today)
        rows.append({**v, "employee_name": record["employee_name"], "job_title": record["job_title"],
                     "manager_name": record["manager_name"],
                     "attention": v["status"] == "pending_leader" or v["decision_due"] or
                     any(k["state"] == "overdue" for k in v["checkpoints"])})
    open_rows = [r for r in rows if r["open"]]
    return {"rows": rows, "open": len(open_rows), "pending_leader": sum(1 for r in rows if r["status"] == "pending_leader"),
            "decision_due": sum(1 for r in rows if r["decision_due"]),
            "overdue_checkins": sum(1 for r in open_rows for k in r["checkpoints"] if k["state"] == "overdue"),
            "confirmed": sum(1 for r in rows if r["status"] == "confirmed"),
            "not_confirmed": sum(1 for r in rows if r["status"] == "not_confirmed")}
