"""Performance status engine: on track / off track for every employee.

Deterministic and explainable -- no LLM. Five factors, each scored 0-100 and
weighted; only factors that have data take part, so a missing review or an
employee who was never assessed is not marked down for it:

  skills       30  share of assessed role skills at target, less 10 per critical gap (TNI)
  review       25  the latest review the manager shared (drafts never count)
  assessment   20  the latest Talent360i assessment a manager decided
  evidence     15  confirmed evidence shared with the employee, last 180 days,
                   recency-weighted (appreciation/achievement up, escalation/concern down)
  development  10  development actions completed vs overdue

Bands (the four performance tiers):
  Exceeding  >= 80  "Best"     On track  >= 65  "Good"
  At risk    >= 50  "Low"      Off track  < 50  "Poor"
  Not enough data   fewer than two factors.
A shared review of 2/5 caps the band at At risk and 1/5 at Off track: the
manager's documented judgement outweighs the arithmetic.

Fairness rules that are part of the engine, not the UI:
  - only evidence the employee can see (shared) moves the score;
  - pulse check-ins are a wellbeing signal and never change the score;
  - every band comes with the factors and reasons that produced it.

On top of the band it derives signals (promotion ready, performance concern,
probation, escalation, 1:1 overdue, wellbeing), the combined promotion-
readiness level (skills readiness for the next role + performance), and a
recommendation playbook. refresh() writes the result to the employee_status
snapshot and applies the escalation rules; org-wide pages read the snapshot.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from services import db, engagement, lifecycle_store as store, progression, tni

WEIGHTS = {"skills": 30, "review": 25, "assessment": 20, "evidence": 15, "development": 10}
FACTOR_LABELS = {"skills": "Skills vs role target", "review": "Latest performance review",
                 "assessment": "Latest assessment", "evidence": "Evidence (180 days)",
                 "development": "Development follow-through"}
EXCEEDING, ON_TRACK, AT_RISK = 80, 65, 50
BANDS = ("Exceeding", "On track", "At risk", "Off track", "Not enough data")
TIERS = {"Exceeding": "Best", "On track": "Good", "At risk": "Low", "Off track": "Poor"}
BAND_TONE = {"Exceeding": "best", "On track": "good", "At risk": "low", "Off track": "poor", "Not enough data": "none"}
REVIEW_SCORES = {1: 10, 2: 30, 3: 60, 4: 80, 5: 95}
EVIDENCE_WINDOW_DAYS = 180
HALF_LIFE_DAYS = 90
ONE_ON_ONE_DAYS = 45
PROBATION_DECISION_DAYS = 30
CHUNK = 500
STALE_MINUTES = 15
ATTENTION_ORDER = {"Off track": 0, "At risk": 1, "Not enough data": 2, "On track": 3, "Exceeding": 4}
SIGNAL_ORDER = ("escalation", "performance_concern", "probation_decision", "probation", "promotion_ready",
                "wellbeing", "one_on_one_overdue")
LEADER_ESCALATION_DAYS = 30


def _clamp(value: float) -> int:
    return int(round(max(0, min(100, value))))


def _factor(key: str, score: float, detail: str) -> dict:
    return {"key": key, "label": FACTOR_LABELS[key], "score": _clamp(score), "weight": WEIGHTS[key], "detail": detail}


def _days(since: str | None, today: date) -> int | None:
    if not since:
        return None
    try:
        return (today - date.fromisoformat(since[:10])).days
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Factors (pure)
# ---------------------------------------------------------------------------

def skills_factor(plan: dict | None) -> dict | None:
    """Average attainment of the role's targets (L2 against an L3 target = 67%), less 5 per
    critical skill two or more levels short. "At target or not" alone would score someone one
    level short everywhere at zero."""
    summary = (plan or {}).get("summary") or {}
    if not summary.get("assessed"):
        return None
    assessed = [s for s in plan["skills"] if s["assessed"] and s["target"]]
    attainment = 100 * sum(min(s["current"], s["target"]) / s["target"] for s in assessed) / len(assessed)
    critical = [s["skill"] for s in assessed if s["is_critical"] and (s["gap"] or 0) >= 2]
    detail = f"{summary['at_target']} of {summary['assessed']} assessed skills at target; {round(attainment)}% of target levels"
    if critical:
        detail += f"; critical gap: {', '.join(critical[:2])}"
    return _factor("skills", attainment - 5 * min(len(critical), 3), detail)


def assessment_factor(cycle: dict | None) -> dict | None:
    if not cycle or not cycle.get("manager_decision"):
        return None
    base = {"approved": 85, "rejected": 35, "sent_back": 50}.get(cycle["manager_decision"])
    if base is None:
        return None
    parts = [progression.decision_label(cycle["manager_decision"])]
    if cycle.get("mcq_pct") is not None:
        base += max(-15, min(15, (cycle["mcq_pct"] - 70) * 0.3))
        parts.append(f"MCQ {round(cycle['mcq_pct'])}%")
    if cycle.get("integrity_risk") == "high":
        base -= 15
        parts.append("high AI-use risk")
    parts.append((cycle.get("updated_at") or "")[:10])
    return _factor("assessment", base, " · ".join(p for p in parts if p))


def review_factor(review: dict | None) -> dict | None:
    if not review or not review.get("rating"):
        return None
    return _factor("review", REVIEW_SCORES.get(review["rating"], 60), f"Rated {review['rating']}/5 for {review['period']}")


def evidence_factor(evidence: list[dict], today: date) -> dict | None:
    positive = negative = 0.0
    counts = {"positive": 0, "negative": 0}
    for item in evidence:
        age = _days(item["occurred_on"], today)
        if not item.get("shared") or age is None or age > EVIDENCE_WINDOW_DAYS or age < 0:
            continue
        weight = (item.get("weight") or 1) * 0.5 ** (age / HALF_LIFE_DAYS)
        if item["sentiment"] == "positive":
            positive += weight
            counts["positive"] += 1
        elif item["sentiment"] == "negative":
            negative += weight * (1.5 if item["kind"] == "escalation" else 1.0)
            counts["negative"] += 1
    if not counts["positive"] and not counts["negative"]:
        return None
    score = 50 + 50 * (positive - negative) / (positive + negative + 1)
    checkins = sum(1 for item in evidence if item.get("checkin"))
    return _factor("evidence", score, f"{counts['positive']} positive, {counts['negative']} negative item(s)"
                   + (f", incl. {checkins} probation check-in(s)" if checkins else ""))


def development_factor(actions: list[dict], today: date) -> dict | None:
    due = [a for a in actions if a["status"] != "cancelled" and a.get("due_date") and a["due_date"] <= today.isoformat()]
    done = sum(1 for a in due if a["status"] == "done")
    overdue = len(due) - done
    if not due:
        return None
    return _factor("development", 100 * done / len(due), f"{done} of {len(due)} due action(s) done" +
                   (f"; {overdue} overdue" if overdue else ""))


def band_for(score: int | None, factors: int, review: dict | None) -> str:
    if score is None or factors < 2:
        return "Not enough data"
    band = ("Exceeding" if score >= EXCEEDING else "On track" if score >= ON_TRACK else
            "At risk" if score >= AT_RISK else "Off track")
    rating = (review or {}).get("rating")
    if rating == 1:
        band = "Off track"
    elif rating == 2 and band in ("Exceeding", "On track"):
        band = "At risk"
    return band


# ---------------------------------------------------------------------------
# Signals, promotion level, recommendations (pure)
# ---------------------------------------------------------------------------

def overdue_actions(actions: list[dict], today: date) -> list[dict]:
    return [a for a in actions if a["status"] in ("open", "in_progress") and a.get("due_date")
            and a["due_date"] < today.isoformat()]


def probation_state(record: dict | None, checkpoints: list[dict], today: date) -> dict | None:
    if not record or record["status"] not in ("active", "pending_leader"):
        return None
    end = date.fromisoformat(record["end_date"])
    start = date.fromisoformat(record["start_date"])
    days_left = (end - today).days
    overdue = [k for k in checkpoints if not k.get("completed_at") and k["due_date"] < today.isoformat()]
    span = max((end - start).days, 1)
    return {"probation_id": record["probation_id"], "status": record["status"], "start_date": record["start_date"],
            "end_date": record["end_date"], "days_left": days_left, "extensions": record.get("extensions", 0),
            "progress_pct": _clamp(100 * (today - start).days / span),
            "checkpoints_done": sum(1 for k in checkpoints if k.get("completed_at")), "checkpoints": len(checkpoints),
            "overdue_checkpoints": [k["label"] for k in overdue],
            "decision_due": record["status"] == "active" and days_left <= PROBATION_DECISION_DAYS,
            "recommendation": record.get("recommendation")}


def promotion_level(readiness: dict | None, band: str, review: dict | None, escalations: list[dict]) -> dict:
    """Combined promotion readiness: next-role skills readiness AND performance."""
    if not readiness or not readiness.get("next"):
        return {"level": "Top of ladder" if readiness and readiness.get("current") else "No role on file", "reason": ""}
    target = readiness["next"]["role_name"]
    if band in ("At risk", "Off track"):
        return {"level": "On hold", "reason": f"Performance is {band.lower()}; resolve that before a move to {target}."}
    if escalations:
        return {"level": "On hold", "reason": "An open performance escalation needs closing first."}
    rating = (review or {}).get("rating")
    skills_band = readiness.get("band")
    if skills_band == "Ready" and band in ("Exceeding", "On track") and (rating is None or rating >= 4):
        why = f"{readiness['pct']}% of {target} skills at target and performance {band.lower()}"
        return {"level": "Ready now", "reason": why + ("" if rating else "; no review on file yet") + "."}
    if skills_band in ("Ready", "Developing"):
        return {"level": "Ready in 6-12 months",
                "reason": f"{readiness['pct']}% of {target} skills at target" +
                          (f"; latest review {rating}/5" if rating and rating < 4 else "") + "."}
    return {"level": "Not yet", "reason": f"{readiness['pct'] or 0}% of {target} skills at target."}


def signals_for(ctx: dict, band: str, promotion: dict, today: date) -> list[dict]:
    out = []
    if ctx["escalations"]:
        top = "leader" if any(e["level"] == "leader" for e in ctx["escalations"]) else "manager"
        out.append({"key": "escalation", "label": f"Escalated to {top}", "tone": "bad"})
    rating = (ctx["review"] or {}).get("rating")
    negative_90 = sum(1 for e in ctx["evidence"] if e.get("shared") and e["sentiment"] == "negative"
                      and (_days(e["occurred_on"], today) or 999) <= 90)
    if band == "Off track" or (rating is not None and rating <= 2) or (band == "At risk" and negative_90 >= 2):
        out.append({"key": "performance_concern", "label": "Performance concern", "tone": "bad"})
    probation = ctx["probation"]
    if probation:
        if probation["status"] == "pending_leader":
            out.append({"key": "probation_decision", "label": "Probation decision with leader", "tone": "warn"})
        elif probation["decision_due"] or probation["overdue_checkpoints"]:
            label = "Probation decision due" if probation["decision_due"] else "Probation check-in overdue"
            out.append({"key": "probation_decision", "label": label, "tone": "warn"})
        else:
            out.append({"key": "probation", "label": f"Probation · {max(probation['days_left'], 0)}d left", "tone": "info"})
    if promotion["level"] == "Ready now":
        out.append({"key": "promotion_ready", "label": "Promotion ready", "tone": "good"})
    if engagement.wellbeing_flag(ctx["pulses"]):
        out.append({"key": "wellbeing", "label": "Wellbeing check-in", "tone": "warn"})
    if ctx["user"].get("manager_id"):
        # Nudge where it matters: a 1:1 rhythm that has lapsed, or anyone at risk or off track.
        last = max((e["occurred_on"] for e in ctx["evidence"] if e["kind"] == "one_on_one"), default=None)
        gap = _days(last, today)
        if (gap is not None and gap > ONE_ON_ONE_DAYS) or (gap is None and band in ("At risk", "Off track")):
            out.append({"key": "one_on_one_overdue",
                        "label": "No 1:1 logged" if gap is None else f"Last 1:1 {gap}d ago", "tone": "warn"})
    return sorted(out, key=lambda s: SIGNAL_ORDER.index(s["key"]))


def recommendations_for(ctx: dict, band: str, signals: list[dict], promotion: dict, today: date) -> list[dict]:
    plan = ctx["plan"] or {}
    gaps = [s["skill"] for s in plan.get("path", [])][:2]
    gap_text = " and ".join(gaps) or "the role's priority skills"
    keys = {s["key"] for s in signals}
    overdue = overdue_actions(ctx["actions"], today)
    open_actions = [a for a in ctx["actions"] if a["status"] in ("open", "in_progress")]
    recs: list[dict] = []

    def add(audience, title, detail, signal=None):
        recs.append({"audience": audience, "title": title, "detail": detail, "signal": signal})

    if "wellbeing" in keys:
        add("manager", "Check workload and wellbeing first",
            "Recent pulse check-ins show low mood or a heavy workload. Talk about support before performance.", "wellbeing")
    if band == "Exceeding":
        add("manager", "Recognise the impact", "Thank them specifically and log it as appreciation evidence.")
        if promotion["level"] == "Ready now":
            add("manager", "Start the promotion path",
                f"Request a promotion-readiness assessment for {ctx['readiness']['next']['role_name']} or send the profile to leadership.")
        else:
            add("manager", "Offer a stretch assignment", f"A visible piece of work that builds {gap_text}.")
        add("employee", "Share what works", "Mentor a colleague or run a short knowledge share on your strongest skill.")
    elif band == "On track":
        if not open_actions:
            add("manager", "Agree the next development step", f"Add one or two actions on {gap_text}.")
        add("manager", "Set one stretch goal", f"Pick a goal that moves {gap_text} up a level this quarter.")
    elif band == "At risk":
        add("manager", "Focus the development plan", f"Agree two or three specific actions on {gap_text} with 30-day check-ins.")
        add("manager", "Weekly 1:1s for six weeks", "Log each one here so progress and support are documented.")
        add("manager", "Write the expectations down", "Confirm what good looks like and check the employee agrees.")
    elif band == "Off track":
        add("manager", "Start a structured 60-day improvement plan",
            "With your HR business partner: specific, observable expectations and weekly checkpoints.")
        add("manager", "Document support given", "Training, coaching and adjustments offered, alongside the evidence.")
        add("leader", "Role-fit review if there is no improvement",
            "If the plan ends without measurable progress, review the case with the manager and HR.")
    else:
        add("manager", "Build the evidence base", "Log 1:1s and evidence, and assign an assessment so the status can be calculated.")
    if overdue:
        add("manager", f"Revisit {len(overdue)} overdue development action(s)",
            "Re-plan with the employee: are the actions still the right ones, and is time protected for them?")
    if "probation_decision" in keys and ctx["probation"]:
        p = ctx["probation"]
        add("manager", "Complete the probation steps",
            f"Probation ends {p['end_date']}. " + ("Record your recommendation." if p["decision_due"] else
                                                   f"Overdue: {', '.join(p['overdue_checkpoints'])}."))
    if "one_on_one_overdue" in keys:
        add("manager", "Schedule a 1:1", "Regular, documented conversations are the strongest evidence for any review.")
    last = ctx["cycle"]
    if last and last.get("integrity_risk") in ("medium", "high"):
        add("manager", "Discuss the AI-use flag", "The last assessment was flagged for possible AI-assisted answers.")
    return recs[:6]


# ---------------------------------------------------------------------------
# Compute one employee from a loaded context (pure)
# ---------------------------------------------------------------------------

def compute(ctx: dict, today: date) -> dict:
    factors = [f for f in (
        skills_factor(ctx["plan"]),
        review_factor(ctx["review"]),
        assessment_factor(ctx["cycle"]),
        evidence_factor(ctx["evidence"], today),
        development_factor(ctx["actions"], today),
    ) if f]
    total_weight = sum(f["weight"] for f in factors)
    score = round(sum(f["score"] * f["weight"] for f in factors) / total_weight) if factors else None
    band = band_for(score, len(factors), ctx["review"])
    promotion = promotion_level(ctx["readiness"], band, ctx["review"], ctx["escalations"])
    signals = signals_for(ctx, band, promotion, today)
    return {
        "user_id": ctx["user"]["user_id"], "band": band, "tier": TIERS.get(band), "tone": BAND_TONE[band],
        "score": score if len(factors) >= 2 else None, "factors": factors,
        "missing": [FACTOR_LABELS[k] for k in WEIGHTS if k not in {f["key"] for f in factors}],
        "signals": signals, "promotion": promotion,
        "recommendations": recommendations_for(ctx, band, signals, promotion, today),
        "readiness_pct": ((ctx["plan"] or {}).get("summary") or {}).get("readiness_pct")
        if ((ctx["plan"] or {}).get("summary") or {}).get("assessed") else None,
    }


# ---------------------------------------------------------------------------
# Loading contexts in bulk (one batch = a fixed number of queries, any size)
# ---------------------------------------------------------------------------

_CHECKIN_TONE = {"on_track": ("positive", 1), "at_risk": ("negative", 1), "off_track": ("negative", 2)}


def _checkin_evidence(checkpoints: list[dict]) -> list[dict]:
    """Completed probation check-ins are documented, shared 1:1s: they count as evidence
    (and as the 1:1 rhythm), so a new joiner has a status before their first review."""
    out = []
    for k in checkpoints:
        if k.get("completed_at") and k.get("rating") in _CHECKIN_TONE:
            sentiment, weight = _CHECKIN_TONE[k["rating"]]
            out.append({"evidence_id": k["checkpoint_id"], "kind": "one_on_one", "checkin": True, "sentiment": sentiment, "weight": weight,
                        "shared": 1, "occurred_on": k["completed_at"][:10], "title": f"Probation {k['label'].lower()}"})
    return out


def _load(user_ids: list[str], today: date) -> dict[str, dict]:
    users = store.users_by_ids(user_ids)
    if not users:
        return {}
    ids = [u["user_id"] for u in users]
    plans = tni.plans_for(users)
    roles = {uid: {k: plan[k] for k in ("role_id", "role_name", "role_grade", "tower")} for uid, plan in plans.items()}
    ladder = progression.role_ladder()
    next_skills: dict[str, list[dict]] = {}
    certs: dict[str, list[dict]] = {}
    for record in db.list_certifications(ids, basis=None):  # newest first
        certs.setdefault(record["user_id"], []).append(record)
    cycles = store.cycles_for_users(ids)
    evidence = store.evidence_for_users(ids, (today - timedelta(days=365)).isoformat())
    reviews = store.latest_reviews(ids)
    actions = store.actions_for_users(ids)
    probations = store.probations_for_users(ids)
    checkpoints = store.checkpoints_for([p["probation_id"] for p in probations.values()])
    pulses = store.pulses_for_users(ids, engagement.week_key(today - timedelta(weeks=4)))
    escalations: dict[str, list[dict]] = {}
    for e in store.list_escalations(user_ids=ids):
        escalations.setdefault(e["user_id"], []).append(e)
    profiles = store.profiles_for(ids)
    previous = store.status_rows(ids)

    contexts = {}
    for user in users:
        uid = user["user_id"]
        role = roles.get(uid)
        nxt = ladder.get(role["role_id"]) if role else None
        if nxt and nxt["role_id"] not in next_skills:
            next_skills[nxt["role_id"]] = db.role_skill_details(nxt["role_id"])
        readiness = progression.readiness_from(
            uid, role, nxt, progression._levels_from(plans.get(uid), certs.get(uid, [])),
            next_skills.get(nxt["role_id"], []) if nxt else [])
        user_cycles = cycles.get(uid, [])
        record = probations.get(uid)
        record_checkpoints = checkpoints.get(record["probation_id"], []) if record else []
        contexts[uid] = {
            "user": user, "plan": plans.get(uid), "role": role, "readiness": readiness,
            "cycle": next((c for c in user_cycles if c.get("manager_decision") and
                           c["status"] in ("manager_approved", "manager_rejected")), None),
            "active_cycle": next((c for c in user_cycles if c["status"] not in ("manager_approved", "manager_rejected")), None),
            "evidence": evidence.get(uid, []) + _checkin_evidence(record_checkpoints),
            "review": reviews.get(uid), "actions": actions.get(uid, []),
            "probation": probation_state(record, record_checkpoints, today),
            "pulses": pulses.get(uid, []), "escalations": escalations.get(uid, []),
            "profile": profiles.get(uid), "previous": previous.get(uid),
        }
    return contexts


# ---------------------------------------------------------------------------
# Escalation rules ("if the problem still exists, tell the manager, then the leader")
# ---------------------------------------------------------------------------

def _apply_escalations(ctx: dict, status: dict, band_since: str, today: date) -> None:
    user = ctx["user"]
    band = status["band"]
    if band in ("Exceeding", "On track"):
        if ctx["escalations"] and store.resolve_open_escalations(user["user_id"], f"Resolved automatically: status back to {band.lower()}."):
            engagement.notify([user.get("manager_id")], "escalation_resolved",
                              f"{user['full_name']} is back {band.lower()}", "Open escalations were closed.",
                              f"/people/{user['user_id']}")
        return
    if band not in ("At risk", "Off track"):
        return
    overdue = overdue_actions(ctx["actions"], today)
    in_band = _days(band_since, today) or 0
    if len(overdue) >= 2 or (not ctx["actions"] and in_band >= 14):
        reason = (f"{band} with {len(overdue)} overdue development actions" if len(overdue) >= 2 else
                  f"{band} for {in_band} days with no development plan")
        if store.open_escalation(user["user_id"], "manager", reason,
                                 "Reset the plan with the employee: two or three achievable actions, weekly check-ins, "
                                 "and documented 1:1s."):
            engagement.notify([user.get("manager_id")], "escalation", f"{user['full_name']}: {reason}",
                              "Talent 360i suggests resetting the development plan.", f"/people/{user['user_id']}#development")
    first_action = min((a["created_at"] for a in ctx["actions"]), default=None)
    plan_age = _days(first_action, today) or 0
    if band == "Off track" and in_band >= LEADER_ESCALATION_DAYS and plan_age >= LEADER_ESCALATION_DAYS:
        reason = f"Off track for {in_band} days despite a development plan started {plan_age} days ago"
        if store.open_escalation(user["user_id"], "leader", reason,
                                 "Review with the manager and HR business partner: a formal performance improvement "
                                 "plan or a role-fit conversation. The decision stays with people, not the system."):
            engagement.notify(engagement.leaders_for(user["user_id"]) + [user.get("manager_id")], "escalation",
                              f"Escalated: {user['full_name']}", reason, f"/people/{user['user_id']}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def refresh(user_ids, today: date | None = None, *, escalate: bool = True) -> dict[str, dict]:
    """Recomputes these employees, writes their snapshot rows and applies the escalation rules."""
    today = today or date.today()
    results: dict[str, dict] = {}
    ids = list(dict.fromkeys(u for u in user_ids if u))
    for start in range(0, len(ids), CHUNK):
        contexts = _load(ids[start:start + CHUNK], today)
        rows = []
        for uid, ctx in contexts.items():
            status = compute(ctx, today)
            prev = ctx["previous"]
            band_since = prev["band_since"] if prev and prev["band"] == status["band"] and prev.get("band_since") else today.isoformat()
            if escalate:
                _apply_escalations(ctx, status, band_since, today)
                if prev and prev["band"] in ("Exceeding", "On track", "Not enough data") and status["band"] in ("At risk", "Off track"):
                    engagement.notify([ctx["user"].get("manager_id")], "status_change",
                                      f"{ctx['user']['full_name']} moved to {status['band'].lower()}",
                                      "; ".join(f["detail"] for f in status["factors"][:2]), f"/people/{uid}")
            profile = ctx["profile"] or {}
            rows.append({
                "user_id": uid, "full_name": ctx["user"]["full_name"], "manager_id": ctx["user"].get("manager_id"),
                "dept_id": profile.get("dept_id"),
                "department": profile.get("department_name") or ctx["user"].get("department"),
                "role_id": (ctx["role"] or {}).get("role_id"),
                "role_name": (ctx["role"] or {}).get("role_name") or ctx["user"].get("job_title"),
                "tower": (ctx["role"] or {}).get("tower"), "band": status["band"], "score": status["score"],
                "readiness_pct": status["readiness_pct"], "promotion_level": status["promotion"]["level"],
                "signals_json": json.dumps(status["signals"]),
                "factors_json": json.dumps([{k: f[k] for k in ("key", "score", "detail")} for f in status["factors"]]),
                "band_since": band_since, "computed_at": _now(),
                # Sort key for "needs attention first", stored so org-wide pages read it from an index.
                "attention_rank": ATTENTION_ORDER[status["band"]] * 1000 + (status["score"] if status["score"] is not None else 999),
            })
            results[uid] = {**status, "context": ctx, "band_since": band_since}
        store.upsert_status(rows)
    return results


def refresh_all(today: date | None = None, *, escalate: bool = True) -> int:
    ids = store.employee_ids()
    refresh(ids, today, escalate=escalate)
    return len(ids)


def refresh_missing(today: date | None = None, limit: int = 2000) -> int:
    """Employees with no snapshot row yet (new hires, first run). Two index counts when there are none."""
    if store.missing_status_count() <= 0:
        return 0
    ids = store.stale_status_ids("0", limit)
    refresh(ids, today)
    return len(ids)


def refresh_stale(today: date | None = None, minutes: int = STALE_MINUTES, limit: int = 5000) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    ids = store.stale_status_ids(cutoff, limit)
    refresh(ids, today)
    return len(ids)


def status_for(user_id: str, today: date | None = None) -> dict | None:
    """Live status for one employee (also refreshes their snapshot row)."""
    return refresh([user_id], today).get(user_id)


MANAGER_ONLY_SIGNALS = ("wellbeing",)


def visible_signals(signals: list[dict], viewer_role: str) -> list[dict]:
    """Wellbeing is between the employee and their line manager: leaders and admins don't see it per person."""
    return signals if viewer_role == "manager" else [s for s in signals if s["key"] not in MANAGER_ONLY_SIGNALS]


def for_viewer(status: dict, viewer_role: str) -> dict:
    if viewer_role == "manager":
        return status
    return {**status, "signals": visible_signals(status["signals"], viewer_role),
            "recommendations": [r for r in status["recommendations"] if r.get("signal") not in MANAGER_ONLY_SIGNALS]}


def card(row: dict, viewer_role: str = "manager") -> dict:
    """The employee-card view model from a snapshot row."""
    name = row.get("full_name") or row["user_id"]
    signals = visible_signals(json.loads(row.get("signals_json") or "[]"), viewer_role)
    return {
        "user_id": row["user_id"], "name": name,
        "initials": "".join(p[0] for p in name.split()[:2]).upper() or "?",
        "role_name": row.get("role_name") or "—", "department": row.get("department") or "—",
        "tower": row.get("tower"), "band": row["band"], "tier": TIERS.get(row["band"]),
        "tone": BAND_TONE.get(row["band"], "none"), "score": row.get("score"),
        "readiness_pct": row.get("readiness_pct"), "promotion_level": row.get("promotion_level"),
        "signals": signals, "factors": json.loads(row.get("factors_json") or "[]"),
        "computed_at": (row.get("computed_at") or "")[:16].replace("T", " "),
    }


def team_cards(manager_id: str, today: date | None = None) -> list[dict]:
    """A manager's direct reports, refreshing anyone whose snapshot is missing or stale."""
    ids = store.employee_ids(manager_id)
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=STALE_MINUTES)).isoformat(timespec="seconds")
    current = store.status_rows(ids)
    stale = [uid for uid in ids if uid not in current or (current[uid]["computed_at"] or "") < cutoff]
    if stale:
        refresh(stale, today)
        current = store.status_rows(ids)
    rows = sorted(current.values(), key=lambda r: (ATTENTION_ORDER.get(r["band"], 5), r.get("score") or 0, r["full_name"] or ""))
    return [card(r) for r in rows]


def band_summary(cards: list[dict]) -> list[dict]:
    counts = {band: 0 for band in BANDS}
    for c in cards:
        counts[c["band"]] = counts.get(c["band"], 0) + 1
    total = len(cards) or 1
    return [{"band": band, "tier": TIERS.get(band), "tone": BAND_TONE[band], "count": counts[band],
             "pct": round(100 * counts[band] / total)} for band in BANDS]


def band_strip(filters: dict) -> list[dict]:
    counts = store.band_counts(filters)
    total = sum(counts.values()) or 1
    return [{"band": b, "tier": TIERS.get(b), "tone": BAND_TONE[b], "count": counts.get(b, 0),
             "pct": round(100 * counts.get(b, 0) / total)} for b in BANDS]


def directory(filters: dict, page: int, sort: str = "attention", page_size: int = 24, viewer_role: str = "leader") -> dict:
    rows, total = store.query_status(filters, page=page, page_size=page_size, sort=sort)
    pages = max(1, -(-total // page_size))
    return {"cards": [card(r, viewer_role) for r in rows], "total": total, "page": min(max(page, 1), pages), "pages": pages,
            "page_size": page_size}


def org_overview(filters: dict) -> dict:
    """Leader view over the snapshot: band mix overall and per department, promotion pipeline."""
    by_band = {band: 0 for band in BANDS}
    departments: dict[str, dict] = {}
    for row in store.status_counts(filters, "department"):
        by_band[row["band"]] = by_band.get(row["band"], 0) + row["n"]
        d = departments.setdefault(row["name"], {"name": row["name"], "total": 0, **{b: 0 for b in BANDS}})
        d[row["band"]] += row["n"]
        d["total"] += row["n"]
    total = sum(by_band.values())
    for d in departments.values():
        d["off_pct"] = round(100 * (d["Off track"] + d["At risk"]) / d["total"]) if d["total"] else 0
    promotion: dict[str, int] = {}
    for row in store.status_counts(filters, "promotion"):
        promotion[row["name"]] = promotion.get(row["name"], 0) + row["n"]
    return {"total": total,
            "bands": [{"band": b, "tier": TIERS.get(b), "tone": BAND_TONE[b], "count": by_band.get(b, 0),
                       "pct": round(100 * by_band.get(b, 0) / total) if total else 0} for b in BANDS],
            "departments": sorted(departments.values(), key=lambda d: (-d["off_pct"], d["name"])),
            "promotion": promotion}


def scrubber(user: dict, limit: int = 400):
    """Replaces the employee's own name tokens with "the employee" (de-identification for the agents)."""
    name_parts = [p for p in (user.get("full_name") or "").split() if len(p) > 1]

    def scrub(text: str) -> str:
        text = str(text or "")
        for part in name_parts:
            text = text.replace(part, "the employee")
        return text[:limit]
    return scrub


def insight_payload(user_id: str, status: dict) -> dict:
    """De-identified input for the A6 insight agent: no name, id, email or manager."""
    ctx = status["context"]
    user = ctx["user"]
    scrub = scrubber(user)

    plan = ctx["plan"] or {}
    return {
        "role_name": (ctx["role"] or {}).get("role_name") or user.get("job_title") or "Unknown role",
        "band": status["band"], "score": status["score"],
        "factors": [{"name": f["label"], "score": f["score"], "detail": scrub(f["detail"])} for f in status["factors"]],
        "skills": [{"skill": s["skill"], "current": s["current"], "target": s["target"], "critical": s["is_critical"]}
                   for s in plan.get("skills", []) if s["assessed"]][:12],
        "next_role": (ctx["readiness"].get("next") or {}).get("role_name") if ctx["readiness"] else None,
        "evidence": [{"kind": e["kind"], "sentiment": e["sentiment"], "date": e["occurred_on"], "title": scrub(e["title"])}
                     for e in ctx["evidence"] if e.get("shared")][:12],
        "review_rating": (ctx["review"] or {}).get("rating"),
        "actions": [{"title": scrub(a["title"]), "status": a["status"], "due": a.get("due_date")} for a in ctx["actions"]][:10],
        "probation": {k: ctx["probation"][k] for k in ("days_left", "checkpoints_done", "checkpoints")} if ctx["probation"] else None,
        # Pulse-derived signals never go to the model.
        "signals": [s["label"] for s in visible_signals(status["signals"], "leader")],
    }
