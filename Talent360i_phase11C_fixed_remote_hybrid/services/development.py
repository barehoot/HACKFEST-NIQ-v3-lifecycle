"""Individual development plan (IDP): actionable items that close skill gaps
and performance issues, with owners, due dates and a success measure.

Where actions come from:
  plan        generated from the employee's own data: the top skill gaps (a real
              catalogue course + supervised practice), escalations or concerns in the
              last 90 days, the latest review's improvement areas, next-role stretch
              work for people on track, and coaching for people at risk
  ai          Agent 6's suggestions, re-checked here (allowed types, the role's own
              skills only, due in 7-120 days) and merged ahead of the generated ones
  manager     added by hand
  one_on_one  action items agreed in a 1:1 (services/evidence.add_manual)

An action is overdue when it is still open past its due date. The escalation
rules in services/performance.py read overdue actions: a plan that is not
moving while the status stays at risk or off track is escalated to the
manager, then to the business leader.
"""
from __future__ import annotations

from datetime import date, timedelta

from domain.governance import sanitise_text
from services import api_client, lifecycle_store as store
from services.backend_api import BackendAPIError

ACTION_LABELS = {"training": "Training", "practice": "On-the-job practice", "mentoring": "Mentoring / coaching",
                 "stretch": "Stretch assignment", "behaviour": "Behaviour", "certification": "Certification",
                 "reading": "Reading / self-study"}
MAX_PLAN_ACTIONS = 6
MIN_DUE_DAYS, MAX_DUE_DAYS = 7, 120


class DevelopmentError(ValueError):
    pass


def _due(today: date, days: int) -> str:
    return (today + timedelta(days=days)).isoformat()


def propose(status: dict, today: date) -> list[dict]:
    """Deterministic plan from the employee's status context (no LLM)."""
    ctx = status["context"]
    plan = ctx["plan"] or {}
    actions: list[dict] = []
    for step in plan.get("path", [])[:3]:
        course = step.get("course")
        if course:
            actions.append({"title": f"Complete {course['short_title']} ({course['course_id']})", "action_type": "training",
                            "skill": step["skill"], "due_date": _due(today, 21),
                            "success_measure": f"Course completed and applied to one live {step['skill']} case."})
        actions.append({"title": f"Apply {step['skill']} on a live case with a reviewer", "action_type": "practice",
                        "skill": step["skill"], "due_date": _due(today, 45),
                        "detail": f"Move from L{step['current']} to L{step['target']}.",
                        "success_measure": f"Manager or SME confirms the L{step['target']} behaviour on the case."})
    cutoff = (today - timedelta(days=90)).isoformat()
    for item in [e for e in ctx["evidence"] if e["sentiment"] == "negative" and e.get("shared") and e["occurred_on"] >= cutoff][:2]:
        actions.append({"title": f"Agree a corrective step for: {item['title'][:80]}", "action_type": "behaviour",
                        "due_date": _due(today, 14), "evidence_id": item["evidence_id"],
                        "success_measure": "No repeat of the issue over the next 60 days."})
    review = ctx["review"]
    if review and (review.get("improvements") or "").strip():
        actions.append({"title": f"Act on review focus: {review['improvements'].strip()[:90]}", "action_type": "behaviour",
                        "due_date": _due(today, 60), "success_measure": "Progress discussed and evidenced at the next 1:1."})
    readiness = ctx["readiness"] or {}
    if status["band"] in ("Exceeding", "On track") and readiness.get("next"):
        unmet = [r for r in readiness["rows"] if not r["met"]][:1]
        for row in unmet:
            actions.append({"title": f"Stretch task in {row['skill']} towards {readiness['next']['role_name']}",
                            "action_type": "stretch", "skill": row["skill"], "due_date": _due(today, 60),
                            "success_measure": f"Delivered at L{row['target']} with the manager's sign-off."})
    if status["band"] in ("At risk", "Off track"):
        actions.append({"title": "Weekly coaching with a mentor or buddy", "action_type": "mentoring",
                        "due_date": _due(today, 30), "success_measure": "Four sessions held and logged as 1:1s."})
    return actions


def _verified_ai_actions(raw: list, skills: set[str], today: date) -> list[dict]:
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        action_type = item.get("action_type")
        skill = item.get("skill")
        title = sanitise_text(item.get("title"), 140)
        try:
            days = int(item.get("due_in_days"))
        except (TypeError, ValueError):
            continue
        if action_type not in ACTION_LABELS or not title or not MIN_DUE_DAYS <= days <= MAX_DUE_DAYS:
            continue
        if skill and str(skill).lower() not in skills:
            skill = None  # an invented skill is dropped, the action kept
        out.append({"title": title, "action_type": action_type, "skill": skill, "due_date": _due(today, days),
                    "success_measure": sanitise_text(item.get("success_measure"), 240), "source": "ai"})
    return out


def generate(user_id: str, by: str, today: date, *, use_ai: bool = True) -> dict:
    from services import performance  # lazy: performance -> engagement -> ... avoids an import cycle

    status = performance.status_for(user_id, today)
    if not status:
        raise DevelopmentError("Employee not found.")
    proposed = [{**a, "source": "plan"} for a in propose(status, today)]
    generated_by = "rules"
    if use_ai:
        skills = {s["skill"].lower() for s in (status["context"]["plan"] or {}).get("skills", [])}
        try:
            insight = api_client.backend_performance_insight(performance.insight_payload(user_id, status), mode="plan") or {}
        except BackendAPIError:
            insight = {}
        ai_actions = _verified_ai_actions(insight.get("development_actions"), skills, today)
        if ai_actions:
            proposed = ai_actions[:4] + proposed
            generated_by = "agent" if insight.get("generated_by") == "agent" else "rules"
    # One action per title, and per (type, skill): an AI "practice X" wins over the generated one.
    open_actions = store.list_actions(user_id, include_closed=False)
    titles = {a["title"].lower() for a in open_actions}
    type_skills = {(a["action_type"], a["skill"].lower()) for a in open_actions if a.get("skill")}
    chosen = []
    for action in proposed:
        type_skill = (action["action_type"], (action.get("skill") or "").lower())
        if action["title"].lower() in titles or (action.get("skill") and type_skill in type_skills):
            continue
        titles.add(action["title"].lower())
        if action.get("skill"):
            type_skills.add(type_skill)
        chosen.append(action)
        if len(chosen) >= MAX_PLAN_ACTIONS:
            break
    ids = store.add_actions(user_id, chosen, by)
    performance.refresh([user_id], today)
    return {"created": len(ids), "generated_by": generated_by}


def add(user_id: str, *, title: str, action_type: str, skill: str | None, due_date: str, detail: str,
        success_measure: str, by: str, today: date, source: str = "manager") -> str:
    title = sanitise_text(title, 140)
    if len(title) < 5 or action_type not in ACTION_LABELS:
        raise DevelopmentError("Give the action a clear title and a type.")
    try:
        due = date.fromisoformat(due_date)
    except ValueError as exc:
        raise DevelopmentError("Choose a due date.") from exc
    if due < today:
        raise DevelopmentError("Choose today or a future due date.")
    return store.add_actions(user_id, [{
        "title": title, "action_type": action_type, "skill": sanitise_text(skill, 120) or None, "due_date": due.isoformat(),
        "detail": sanitise_text(detail, 600) or None, "success_measure": sanitise_text(success_measure, 240) or None,
        "source": source}], by)[0]


def add_from_one_on_one(user_id: str, items: list[str], evidence_id: str, by: str, today: date) -> list[str]:
    return store.add_actions(user_id, [{"title": item[:140], "action_type": "practice", "source": "one_on_one",
                                        "due_date": _due(today, 14), "evidence_id": evidence_id,
                                        "success_measure": "Reviewed at the next 1:1."} for item in items if item], by)


def view(user_id: str, today: date) -> dict:
    actions = store.list_actions(user_id)
    for a in actions:
        a["type_label"] = ACTION_LABELS.get(a["action_type"], a["action_type"])
        a["overdue"] = a["status"] in ("open", "in_progress") and bool(a.get("due_date")) and a["due_date"] < today.isoformat()
    live = [a for a in actions if a["status"] != "cancelled"]
    done = sum(1 for a in live if a["status"] == "done")
    return {"actions": actions, "open": [a for a in live if a["status"] in ("open", "in_progress")],
            "progress": {"done": done, "total": len(live), "overdue": sum(1 for a in live if a["overdue"]),
                         "pct": round(100 * done / len(live)) if live else 0}}
