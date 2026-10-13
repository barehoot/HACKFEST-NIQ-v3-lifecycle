"""Leader insights: the organisation report, live skill distribution and
critical-skill coverage, and calibration of manager decisions.

Everything here is computed from live state -- the TNI baseline
(services/tni.py), assessment cycles, scheduled training and leader
calibration reviews. Read-only: nothing here changes a level or a decision.
"""
from __future__ import annotations

from datetime import date, timedelta

from services import db, tni

# Same tower -> framework mapping as the pipeline API (app/main.py TOWER_FRAMEWORK).
FRAMEWORK_TOWERS = {"finops": ("AP", "O2C"), "rd": ("R2R",)}
LEVEL_NAMES = {1: "Awareness", 2: "Working Knowledge", 3: "Proficient", 4: "Advanced", 5: "Expert / Strategic"}

CYCLE_STAGES = [
    ("pending_sme_setup", "SME set-up"),
    ("pending_sme_approval", "SME question review"),
    ("ready_for_assessment", "Ready to take"),
    ("mcq_submitted", "Simulation in progress"),
    ("pending_manager_approval", "Manager review"),
    ("manager_approved", "Approved"),
    ("manager_rejected", "Rejected / sent back"),
]
DECISION_LABELS = {"approved": "Approved · certified", "rejected": "Rejected · gap training", "sent_back": "Sent back to retake"}


def _pct(part, whole) -> int:
    return round(100 * part / whole) if whole else 0


def _group(people: list[dict], key) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for person in people:
        groups.setdefault(key(person) or "—", []).append(person)
    rows = []
    for name, members in groups.items():
        summary = tni.summarise([s for p in members for s in p["skills"]])
        rows.append({"name": name, "people": len(members), **summary})
    return sorted(rows, key=lambda r: (r["readiness_pct"], r["name"]))


def _level_distribution(people: list[dict]) -> list[dict]:
    ratings = [s["current"] for p in people for s in p["skills"] if s["assessed"]]
    return [
        {"level": level, "label": f"L{level} · {LEVEL_NAMES[level]}", "count": ratings.count(level),
         "value": _pct(ratings.count(level), len(ratings))}
        for level in LEVEL_NAMES
    ]


def _top_gaps(people: list[dict], limit: int = 10) -> list[dict]:
    by_skill: dict[str, dict] = {}
    for person in people:
        for step in person["path"]:
            row = by_skill.setdefault(step["skill"], {"skill": step["skill"], "people": 0, "high": 0, "total_gap": 0,
                                                      "covered": 0, "critical": False, "course": None})
            row["people"] += 1
            row["high"] += step["severity"] == "High"
            row["total_gap"] += step["gap"]
            row["covered"] += bool(step["course"])
            row["critical"] = row["critical"] or step["is_critical"]
            row["course"] = row["course"] or step["course"]
    rows = sorted(by_skill.values(), key=lambda r: (-r["high"], -r["people"], r["skill"]))[:limit]
    for row in rows:
        row["avg_gap"] = round(row["total_gap"] / row["people"], 1)
    return rows


def _framework_people(framework: str) -> list[dict]:
    towers = FRAMEWORK_TOWERS.get(framework, FRAMEWORK_TOWERS["finops"])
    return [p for p in tni.org_tni()["people"] if p["tower"] in towers]


# ---------------------------------------------------------------------------
# Organisation report
# ---------------------------------------------------------------------------

def org_report(today: date) -> dict:
    org = tni.org_tni()
    people, reports = org["people"], org["reports"]
    summary = org["summary"] or tni.summarise([])
    users = {u["user_id"]: u for u in db.list_users()}
    cycles = db.list_all_cycles()
    events = db.list_events("0000-01-01", "9999-12-31", include_cancelled=True)
    reviews = db.list_calibration_reviews()

    latest_cycle: dict[str, dict] = {}
    for cycle in sorted(cycles, key=lambda c: c["created_at"]):
        latest_cycle[cycle["user_id"]] = cycle
    stage_counts = {status: 0 for status, _ in CYCLE_STAGES}
    for cycle in latest_cycle.values():
        if cycle["status"] in stage_counts:
            stage_counts[cycle["status"]] += 1
    pipeline = [{"status": "not_started", "label": "Not started", "count": len(reports) - len(latest_cycle)}]
    pipeline += [{"status": status, "label": label, "count": stage_counts[status]} for status, label in CYCLE_STAGES]
    peak = max((row["count"] for row in pipeline), default=0)
    for row in pipeline:
        row["pct"] = _pct(row["count"], peak)

    training = [e for e in events if e["event_type"] == "training"]
    live_training = [e for e in training if e["status"] != "cancelled"]
    due = [e for e in live_training if e["event_date"] <= today.isoformat()]
    horizon = (today + timedelta(days=30)).isoformat()
    courses: dict[str, dict] = {}
    for e in live_training:
        row = courses.setdefault(e["course_id"] or e["title"], {"course_id": e["course_id"], "title": tni.short_course_title(e["title"]),
                                                               "enrolled": 0, "completed": 0})
        row["enrolled"] += 1
        row["completed"] += e["status"] == "completed"
    training_summary = {
        "enrolled": len(live_training),
        "people": len({e["user_id"] for e in live_training}),
        "completed": sum(1 for e in training if e["status"] == "completed"),
        "cancelled": sum(1 for e in training if e["status"] == "cancelled"),
        "upcoming_30d": sum(1 for e in live_training if e["status"] == "scheduled" and today.isoformat() <= e["event_date"] <= horizon),
        "completion_pct": _pct(sum(1 for e in due if e["status"] == "completed"), len(due)),
        "assessments_booked": sum(1 for e in events if e["event_type"] == "assessment" and e["status"] != "cancelled"),
        "top_courses": sorted(courses.values(), key=lambda c: (-c["enrolled"], c["title"]))[:8],
    }

    decided = [c for c in cycles if c["manager_decision"]]
    decisions = {
        "approved": sum(1 for c in decided if c["manager_decision"] == "approved"),
        "sent_back": sum(1 for c in decided if c["manager_decision"] != "approved"),  # rejected or sent back
        "concur": sum(1 for r in reviews if r["outcome"] == "concur"),
        "rereview": sum(1 for r in reviews if r["outcome"] == "rereview"),
    }

    by_manager = []
    for manager in (u for u in users.values() if u["role"] == "manager"):
        team_ids = {r["user_id"] for r in reports if r["manager_id"] == manager["user_id"]}
        team = [p for p in people if p["user_id"] in team_ids]
        team_summary = tni.summarise([s for p in team for s in p["skills"]])
        mine = [c for c in decided if c["manager_id"] == manager["user_id"]]
        team_training = [e for e in live_training if e["user_id"] in team_ids]
        by_manager.append({
            "name": manager["full_name"], "team": len(team_ids), "readiness_pct": team_summary["readiness_pct"],
            "gaps": team_summary["gaps"], "high": team_summary["high"],
            "decisions": len(mine), "approved": sum(1 for c in mine if c["manager_decision"] == "approved"),
            "training": len(team_training), "training_completed": sum(1 for e in team_training if e["status"] == "completed"),
        })

    return {
        "kpis": {
            "employees": len(reports), "with_tni": len(people),
            "managers": sum(1 for u in users.values() if u["role"] == "manager"),
            "readiness_pct": summary["readiness_pct"], "gaps": summary["gaps"], "high": summary["high"],
            "assessed_pct": _pct(summary["assessed"], summary["total"]), "not_assessed": summary["not_assessed"],
            "coverage_pct": summary["coverage_pct"],
            "active_cycles": sum(1 for c in latest_cycle.values() if c["status"] not in ("manager_approved", "manager_rejected")),
        },
        "by_tower": _group(people, lambda p: p["tower"]),
        "by_role": _group(people, lambda p: p["role_name"]),
        "by_manager": sorted(by_manager, key=lambda m: -m["team"]),
        "top_gaps": _top_gaps(people),
        "distribution": _level_distribution(people),
        "pipeline": pipeline,
        "training": training_summary,
        "decisions": decisions,
        "generated": today.isoformat(),
    }


def org_export_rows(today: date) -> list[dict]:
    """One row per employee for the CSV download."""
    org = tni.org_tni()
    by_id = {p["user_id"]: p for p in org["people"]}
    users = {u["user_id"]: u for u in db.list_users()}
    latest: dict[str, dict] = {}
    for cycle in sorted(db.list_all_cycles(), key=lambda c: c["created_at"]):
        latest[cycle["user_id"]] = cycle
    stage = dict(CYCLE_STAGES)
    events = db.list_events("0000-01-01", "9999-12-31")
    rows = []
    for report in org["reports"]:
        person = by_id.get(report["user_id"])
        s = person["summary"] if person else tni.summarise([])
        mine = [e for e in events if e["user_id"] == report["user_id"] and e["event_type"] == "training"]
        cycle = latest.get(report["user_id"])
        rows.append({
            "employee_id": report["user_id"], "employee": report["full_name"],
            "manager": (users.get(report["manager_id"]) or {}).get("full_name", ""),
            "role": person["role_name"] if person else report["job_title"], "tower": person["tower"] if person else "",
            "skills_total": s["total"], "skills_assessed": s["assessed"], "skills_at_target": s["at_target"],
            "open_gaps": s["gaps"], "high_gaps": s["high"], "readiness_pct": s["readiness_pct"] if s["assessed"] else "",
            "latest_assessment": stage.get(cycle["status"], cycle["status"]) if cycle else "Not started",
            "training_booked": sum(1 for e in mine if e["status"] == "scheduled"),
            "training_completed": sum(1 for e in mine if e["status"] == "completed"),
        })
    return rows


def leader_dashboard(today: date) -> dict:
    report = org_report(today)
    k = report["kpis"]
    return {
        "metrics": {"employees": k["employees"], "assessment_coverage": f"{k['assessed_pct']}%",
                    "open_gaps": k["gaps"], "learning_plans": report["training"]["people"]},
        "top_gaps": [{"skill": g["skill"], "employees": g["people"], "high": g["high"]} for g in report["top_gaps"][:3]],
        "report": report,
    }


# ---------------------------------------------------------------------------
# Distribution and critical-skill coverage (live, per framework)
# ---------------------------------------------------------------------------

def distribution(framework: str) -> list[dict]:
    return _level_distribution(_framework_people(framework))


def critical_coverage(framework: str) -> list[dict]:
    """Per skill: share of assessed people at or above their role target (critical skills first)."""
    by_skill: dict[str, dict] = {}
    for person in _framework_people(framework):
        for s in person["skills"]:
            if not s["assessed"]:
                continue
            row = by_skill.setdefault(s["skill"], {"skill": s["skill"], "critical": False, "assessed": 0, "at_target": 0})
            row["critical"] = row["critical"] or s["is_critical"]
            row["assessed"] += 1
            row["at_target"] += s["gap"] == 0
    rows = []
    for row in by_skill.values():
        coverage = _pct(row["at_target"], row["assessed"])
        rows.append({"skill": row["skill"], "criticality": "Critical" if row["critical"] else "Standard",
                     "coverage": coverage, "target": 100, "gap": 100 - coverage,
                     "affected": row["assessed"] - row["at_target"], "assessed": row["assessed"]})
    return sorted(rows, key=lambda r: (r["criticality"] != "Critical", r["coverage"], r["skill"]))


def framework_insights(framework: str) -> dict:
    coverage = critical_coverage(framework)
    critical = [c for c in coverage if c["criticality"] == "Critical"]
    dist = distribution(framework)
    people = _framework_people(framework)
    summary = tni.summarise([s for p in people for s in p["skills"]])
    return {
        "framework": framework, "distribution": dist, "coverage": coverage,
        "headline": {
            "critical_skills_below_target": sum(1 for c in critical if c["coverage"] < 100),
            "average_critical_coverage": round(sum(c["coverage"] for c in critical) / len(critical)) if critical else 0,
            "latest_highest_level_share": dist[-1]["value"] + dist[-2]["value"] if dist else 0,
            "assessed_pct": _pct(summary["assessed"], summary["total"]),
            "people": len(people),
        },
    }


# ---------------------------------------------------------------------------
# Calibration of manager decisions
# ---------------------------------------------------------------------------

def _divergence(cycle: dict) -> str | None:
    """Decision that runs against the evidence band -- the bias signal leaders review first."""
    band = (cycle.get("evidence_report") or {}).get("compatibility_band")
    if cycle["manager_decision"] == "approved" and band == "Not yet ready":
        return "Approved despite weak evidence"
    if cycle["manager_decision"] != "approved" and band == "Strong fit":
        return "Not approved despite strong evidence"
    return None


def calibration(manager_filter: str | None = None) -> dict:
    decided = db.list_decided_cycles()
    reviews = db.list_calibration_reviews()
    latest_review: dict[str, dict] = {}
    for review in reviews:  # newest first
        latest_review.setdefault(review["cycle_id"], review)

    queue = []
    for cycle in decided:
        evidence = cycle.get("evidence_report") or {}
        review = latest_review.get(cycle["cycle_id"])
        reopened = cycle["status"] == "pending_manager_approval"
        queue.append({
            "cycle_id": cycle["cycle_id"], "employee": cycle["employee_name"], "role": cycle["matched_role_name"],
            "manager_id": cycle["decided_by"], "manager": cycle["manager_name"] or cycle["decided_by"] or "—",
            "decision": cycle["manager_decision"], "decision_label": DECISION_LABELS.get(cycle["manager_decision"], cycle["manager_decision"]),
            "note": cycle.get("manager_decision_notes") or "",
            "band": (evidence.get("compatibility_band") or "—").replace("--", "—"),
            "compatibility": evidence.get("compatibility_score_pct"),
            "evidence_score": evidence.get("evidence_score_pct"),
            "mcq": (cycle.get("mcq_score") or {}).get("overall_score_pct"),
            "divergence": _divergence(cycle),
            "no_rationale": not (cycle.get("manager_decision_notes") or "").strip(),
            "review": review, "reopened": reopened,
            "reviewable": cycle["status"] in ("manager_approved", "manager_rejected"),
            "decided_at": cycle["updated_at"][:10],
        })
    queue.sort(key=lambda r: (r["review"] is not None and not r["reopened"], r["divergence"] is None, r["decided_at"]), reverse=False)

    managers: dict[str, dict] = {}
    for row in queue:
        m = managers.setdefault(row["manager"], {"manager": row["manager"], "reviews": 0, "approved": 0, "sent_back": 0,
                                                 "divergent": 0, "no_rationale": 0, "concur": 0, "rereview": 0,
                                                 "compat_approved": [], "compat_sent_back": []})
        m["reviews"] += 1
        key = "approved" if row["decision"] == "approved" else "sent_back"
        m[key] += 1
        if row["compatibility"] is not None:
            m[f"compat_{key}"].append(row["compatibility"])
        m["divergent"] += bool(row["divergence"])
        m["no_rationale"] += row["no_rationale"]
    for review in reviews:
        name = review["manager_name"] or review["manager_id"] or "—"
        if name in managers:
            managers[name][review["outcome"]] += 1
    patterns = []
    for m in managers.values():
        m["approval_rate"] = _pct(m["approved"], m["reviews"])
        m["avg_compat_approved"] = round(sum(m["compat_approved"]) / len(m["compat_approved"])) if m["compat_approved"] else None
        m["avg_compat_sent_back"] = round(sum(m["compat_sent_back"]) / len(m["compat_sent_back"])) if m["compat_sent_back"] else None
        notes = []
        if m["divergent"]:
            notes.append(f"{m['divergent']} of {m['reviews']} decision(s) run against the evidence band")
        if m["rereview"]:
            notes.append(f"{m['rereview']} sent back for re-review by a leader")
        if m["no_rationale"]:
            notes.append(f"{m['no_rationale']} decision(s) without a written rationale")
        if (m["avg_compat_approved"] is not None and m["avg_compat_sent_back"] is not None
                and m["avg_compat_sent_back"] >= m["avg_compat_approved"]):
            notes.append("sent-back evidence scores are not lower than approved ones")
        m["signal"] = "Review pattern" if (m["divergent"] or m["rereview"] or len(notes) > 1) else "Within range"
        m["note"] = "; ".join(notes).capitalize() + "." if notes else "Decisions are consistent with the evidence bands."
        if m["reviews"] < 3:
            m["note"] += " Low volume — treat as indicative."
        patterns.append(m)

    if manager_filter:
        queue = [r for r in queue if r["manager"] == manager_filter]
    return {
        "queue": queue, "patterns": sorted(patterns, key=lambda m: (m["signal"] != "Review pattern", m["manager"])),
        "totals": {"decisions": len(decided), "divergent": sum(1 for r in queue if r["divergence"]),
                   "reviewed": sum(1 for r in queue if r["review"]), "reopened": sum(1 for r in queue if r["reopened"])},
    }
