"""Performance summaries for a mid-year review, an annual review, a 1:1 or a promotion
discussion (Agent 9), built on a deterministic fact sheet the manager and leader always see.

facts()     everything the record holds for the period, from every source:
              - manager reviews (shared ones)
              - evidence shared with the employee: notes the manager logged (each read by
                Agent 7 for context), Outlook / Teams messages they tagged, peer recognition --
                with the mix by source; private manager notes are counted but never used
              - skills: current levels against the role, levels certified in the period
                ("skills achieved"), the projected learning curve
              - learning: training completed and booked, development actions
              - for a promotion discussion: the rules' readiness for the target role -- the
                next role up, or a career aspiration in another department or vertical
generate()  the same facts, de-identified (no name, id, email or manager), to Agent 9; the
            summary is stored next to the facts it was built from

Windows: mid-year = the last 6 months, annual = 12 months, promotion = 12 months, and a 1:1
covers the time since the last 1:1 note (14-90 days; 30 when there is none).
"""
from __future__ import annotations

from datetime import date, timedelta

from services import api_client, db, development, evidence as evidence_service, lifecycle_store as store, mobility
from services import performance, progression, tni
from services.backend_api import BackendAPIError

TYPES = {"mid_year": "Mid-year review", "annual": "Annual review", "one_on_one": "1:1", "promotion": "Promotion readiness"}
WINDOW_DAYS = {"mid_year": 182, "annual": 365, "promotion": 365}
SOURCE_GROUPS = {"manual": "Manager notes", "outlook": "Tagged email", "email_file": "Tagged email", "teams": "Teams",
                 "peer": "Peer recognition", "integration": "Integration"}
MAX_EVIDENCE = 30


class SummaryError(ValueError):
    pass


def window(review_type: str, user_id: str, today: date) -> tuple[date, date]:
    if review_type == "one_on_one":
        last = next((e for e in store.list_evidence(user_id) if e["kind"] == "one_on_one"), None)
        start = date.fromisoformat(last["occurred_on"]) if last else today - timedelta(days=30)
        start = max(min(start, today - timedelta(days=14)), today - timedelta(days=90))
        return start, today
    return today - timedelta(days=WINDOW_DAYS[review_type]), today


def promotion_targets(user_id: str) -> list[dict]:
    """The next role up the ladder plus every career aspiration with a job profile."""
    current = tni.current_role(user_id)
    out = []
    nxt = progression.next_role(current["role_id"]) if current else None
    if nxt:
        out.append({"role_id": nxt["role_id"], "role_name": nxt["role_name"], "tower": nxt["tower"], "kind": "Next role"})
    for a in store.list_aspirations(user_id=user_id):
        if a["role_id"] and a["role_id"] not in {o["role_id"] for o in out}:
            out.append({"role_id": a["role_id"], "role_name": a["role_name"], "tower": a["tower"], "kind": "Aspiration"})
    return out


def facts(user_id: str, review_type: str, today: date, target_role_id: str | None = None) -> dict:
    if review_type not in TYPES:
        raise SummaryError("Choose a review type.")
    status = performance.status_for(user_id, today)
    if not status:
        raise SummaryError("No status for this employee yet.")
    ctx = status["context"]
    start, end = window(review_type, user_id, today)
    since, until = start.isoformat(), end.isoformat()

    items = [e for e in store.list_evidence(user_id) if since <= e["occurred_on"] <= until]
    shared = [e for e in items if e["shared"]]
    evidence = []
    for n, e in enumerate(shared[:MAX_EVIDENCE], start=1):
        ai = e.get("ai") or {}
        evidence.append({"ref": f"E{n}", "evidence_id": e["evidence_id"], "kind": e["kind"],
                         "kind_label": evidence_service.KIND_LABELS.get(e["kind"], e["kind"]), "sentiment": e["sentiment"],
                         "source": e["source"], "source_label": SOURCE_GROUPS.get(e["source"], e["source"]), "date": e["occurred_on"],
                         "title": e["title"], "context": ai.get("summary") or e["summary"][:300],
                         "skills": list(dict.fromkeys((e.get("skills") or []) + (ai.get("skills") or [])))[:4],
                         "weight": e["weight"], "ai_read": bool(ai)})
    mix: dict[str, int] = {}
    for e in evidence:
        mix[e["source_label"]] = mix.get(e["source_label"], 0) + 1

    reviews = [r for r in store.list_reviews(user_id, include_drafts=False)
               if since <= (r.get("shared_at") or r.get("updated_at") or "")[:10] <= until][:4]
    plan = tni.employee_tni(user_id) or {}
    achieved = [{"skill": c["skill"], "level": c["level"], "previous": c.get("previous_level"), "date": (c["created_at"] or "")[:10]}
                for c in db.list_certifications([user_id]) if since <= (c["created_at"] or "")[:10] <= until][:10]
    curve = plan.get("curve") or {}
    points = curve.get("points") or []
    dev = development.view(user_id, today)
    learning = {"completed": [t["title"] for t in store.completed_training(user_id, since, until)][:8],
                "booked": store.upcoming_training_count(user_id, today.isoformat()),
                "actions_done": dev["progress"]["done"], "actions_open": len(dev["open"]),
                "actions_overdue": dev["progress"]["overdue"],
                "curve": {"today_avg": points[0]["y"] if points else None, "projected_avg": points[-1]["y"] if points else None,
                          "target_avg": (curve.get("target") or {}).get("v"), "weeks": points[-1]["x"] if points else None}}
    promotion = None
    if review_type == "promotion":
        targets = promotion_targets(user_id)
        target_role_id = target_role_id if target_role_id in {t["role_id"] for t in targets} else (targets[0]["role_id"] if targets else None)
        if not target_role_id:
            raise SummaryError("There is no next role or career aspiration to assess promotion readiness against.")
        analysis = mobility.gap_analysis(user_id, target_role_id)
        promotion = {"target_role_id": target_role_id, "target_role": analysis["role"]["role_name"],
                     "level": analysis["promotion"]["level"], "reason": analysis["promotion"]["reason"],
                     "skills_band": analysis["assessed_band"], "pct": analysis["assessed_pct"],
                     "pct_with_cv": analysis["pct"], "critical_missed": analysis["critical_missed"],
                     "cross_department": analysis["cross_vertical"] or analysis["cross_department"]}
    return {
        "review_type": review_type, "label": TYPES[review_type], "window": {"from": since, "to": until},
        "band": status["band"], "score": status["score"], "tone": status["tone"], "role_name": (ctx["role"] or {}).get("role_name"),
        "factors": [{"name": f["label"], "score": f["score"], "detail": f["detail"]} for f in status["factors"]],
        "reviews": [{"period": r["period"], "rating": r["rating"], "summary": (r["summary"] or "")[:400]} for r in reviews],
        "ratings": [{"period": r["period"], "rating": r["rating"]} for r in store.list_reviews(user_id, include_drafts=False) if r["rating"]],
        "evidence": evidence, "mix": mix, "private_notes": len(items) - len(shared),
        "positive": sum(1 for e in evidence if e["sentiment"] == "positive"),
        "negative": sum(1 for e in evidence if e["sentiment"] == "negative"),
        "skills": [{"skill": s["skill"], "current": s["current"], "target": s["target"], "critical": s["is_critical"],
                    "validated": bool(s.get("validated"))} for s in plan.get("skills", []) if s["assessed"]][:15],
        "skills_achieved": achieved, "learning": learning, "promotion": promotion,
        "open_actions": [a["title"] for a in dev["open"]][:6],
    }


def payload(user_id: str, f: dict) -> dict:
    """The fact sheet with the employee's name removed, in Agent 9's request shape."""
    scrub = performance.scrubber(db.get_user(user_id) or {})
    out = {k: f[k] for k in ("review_type", "band", "score", "window", "skills", "skills_achieved")}
    out["role_name"] = f["role_name"] or "their role"
    out["factors"] = [{**x, "detail": scrub(x["detail"])[:300]} for x in f["factors"]]
    out["reviews"] = [{**r, "summary": scrub(r["summary"])} for r in f["reviews"]]
    out["evidence"] = [{"ref": e["ref"], "kind": e["kind"], "sentiment": e["sentiment"], "source": e["source"], "date": e["date"],
                        "title": scrub(e["title"])[:200], "context": scrub(e["context"]), "skills": e["skills"], "weight": e["weight"]}
                       for e in f["evidence"]]
    out["learning"] = {**f["learning"], "completed": [t[:200] for t in f["learning"]["completed"]],
                       "curve": {k: v for k, v in f["learning"]["curve"].items() if v is not None}}
    out["open_actions"] = [scrub(a)[:200] for a in f["open_actions"]]
    if f["promotion"]:
        p = f["promotion"]
        out["promotion"] = {"target_role": p["target_role"], "level": p["level"], "skills_band": p["skills_band"], "pct": p["pct"],
                            "critical_missed": p["critical_missed"], "cross_department": p["cross_department"], "reason": p["reason"]}
    return out


def generate(user_id: str, review_type: str, requested_by: str, today: date, target_role_id: str | None = None) -> str:
    f = facts(user_id, review_type, today, target_role_id)
    try:
        result = api_client.backend_performance_summary(payload(user_id, f)) or {}
    except BackendAPIError as exc:
        raise SummaryError("The AI summary service is unavailable right now. The fact sheet below is still accurate.") from exc
    refs = {e["ref"] for e in f["evidence"]}
    for key in ("highlights", "concerns"):  # re-checked here too: only refs that exist in the facts
        result[key] = [{**i, "refs": [r for r in i.get("refs", []) if r in refs]} for i in result.get(key) or [] if isinstance(i, dict)]
    summary = {k: result.get(k) for k in ("headline", "overall", "highlights", "concerns", "evidence_mix", "skills_growth", "learning",
                                          "development_focus", "talking_points", "suggested_rating", "promotion_view")}
    return store.save_summary(user_id=user_id, review_type=review_type,
                              target_role_id=(f["promotion"] or {}).get("target_role_id"),
                              window=(f["window"]["from"], f["window"]["to"]), facts=f, summary=summary,
                              generated_by=result.get("generated_by") or "agent", requested_by=requested_by)


def review_draft(summary: dict) -> dict:
    """A stored mid-year / annual summary as a starting point for the review form."""
    s = summary["summary"]
    lines = [f"- {i['text']}" for i in s.get("highlights") or []]
    concerns = [f"- {i['text']}" for i in s.get("concerns") or []] + [f"- {t}" for t in s.get("development_focus") or []]
    return {"summary": "\n\n".join(x for x in (s.get("overall"), s.get("evidence_mix")) if x),
            "strengths": "\n".join(lines + [f"- {t}" for t in s.get("skills_growth") or []]),
            "improvements": "\n".join(concerns), "suggested_rating": s.get("suggested_rating"),
            "generated_by": summary.get("generated_by"), "from_summary": True}


def review_period(review_type: str, today: date) -> str:
    return f"{today.year}-FY" if review_type == "annual" else f"{today.year}-H1"
