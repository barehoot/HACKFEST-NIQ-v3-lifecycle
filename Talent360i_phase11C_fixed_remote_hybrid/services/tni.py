"""Training Needs Identification (TNI) read model.

Reads the synthetic Skill_Gaps_TNI cohort (skill_gaps, loaded by
scripts/build_sqlite.py) plus the training catalogue, and shapes it for the
employee "My Skills & TNI" / "My Training Plan" pages and the manager
"Learning & TNI" page: a skill heatmap, the skill set, a projected learning
curve, and recommended learning with gap coverage.

Read-only and deterministic -- no LLM call, and nothing here changes a
person's level. Levels a manager certified from an assessment
(skill_certifications, services/progression.py) take precedence over the
dataset's level when they are higher, and are marked as validated. Employees
with no Skill_Gaps_TNI rows get their role's skills from role_skill_map, all
"Not assessed" until an assessment is certified. The projection reuses Agent
5's own duration model (agents/agent5_devplan.py,
estimate_remediation_duration) so the curve agrees with the weeks the
development plan quotes.
"""
from __future__ import annotations

import json
import re

from services import db

LEVELS = (1, 2, 3, 4, 5)
SEVERITY_RANK = {"High": 2, "Moderate": 1}

# Mirrors agents/agent5_devplan.py: base weeks per gap, +1 if High, +0.5 if critical.
BASE_WEEKS_PER_GAP = 1.5
HIGH_EXTRA_WEEKS = 1.0
CRITICAL_EXTRA_WEEKS = 0.5

# Same "template never filled in" check as agent5_devplan._is_usable_course_title
# (TRN-0031's title is literally "Role-based assessment remediation for "),
# plus titles that are only an editor's note in parentheses (TRN-0021 reads
# "(Title currently reads CI—moving under CI below)").
_UNFILLED_TITLE_RE = re.compile(r"\bfor\s*$", re.IGNORECASE)
_NOTE_ONLY_TITLE_RE = re.compile(r"^\(.*\)$")
_REMEDIATION_PREFIX = "Role-based assessment remediation for "


def _usable(title: str | None) -> bool:
    title = (title or "").strip()
    return bool(title) and not _UNFILLED_TITLE_RE.search(title) and not _NOTE_ONLY_TITLE_RE.match(title)


def short_course_title(title: str) -> str:
    """'Role-based assessment remediation for X' -> 'X remediation' for tight spaces."""
    if title and title.startswith(_REMEDIATION_PREFIX):
        return f"{title[len(_REMEDIATION_PREFIX):]} remediation"
    return title


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------

def _catalogue(conn):
    courses = {
        r["course_id"]: dict(r)
        for r in conn.execute(
            "SELECT course_id, tower, course_title, delivery_type, level_group FROM training_catalogue"
        ).fetchall()
    }
    mapped: dict[str, list[str]] = {}
    for r in conn.execute("SELECT skill_id, course_id FROM training_skill_map ORDER BY training_skill_map_id"):
        mapped.setdefault(r["skill_id"], []).append(r["course_id"])
    return courses, mapped


def pick_course(skill: str, recommended_id: str | None, mapped_ids: list[str], courses: dict) -> dict | None:
    """Best real catalogue course for one skill gap, or None if none exists.

    The dataset's recommended_course_id is shifted for most R2R skills (e.g.
    "Reconciliations & Clearing" points at the Teamwork course), while
    training_skill_map also lists the correctly titled course. So a course
    whose title names the skill wins; then the TNI recommendation; then any
    skill-mapped course. Unusable (half-filled) titles are never offered.
    """
    candidates = []
    for cid in [recommended_id, *mapped_ids]:
        course = courses.get(cid) if cid else None
        if course and _usable(course["course_title"]) and course not in candidates:
            candidates.append(course)
    if not candidates:
        return None
    skill_key = (skill or "").strip().lower()
    by_title = next((c for c in candidates if skill_key and skill_key in c["course_title"].lower()), None)
    if by_title:
        chosen, basis = by_title, "title matches skill"
    elif recommended_id and candidates[0]["course_id"] == recommended_id:
        chosen, basis = candidates[0], "TNI recommendation"
    else:
        chosen, basis = candidates[0], "skill map"
    return {**chosen, "short_title": short_course_title(chosen["course_title"]), "basis": basis}


# ---------------------------------------------------------------------------
# Skill rows
# ---------------------------------------------------------------------------

def _gap_rows(conn, user_ids: list[str]):
    if not user_ids:
        return []
    # The id list travels as one JSON parameter, so the SQL text is fixed.
    # The role's live requirements (role_skill_map, editable on the Job
    # Profiles page) win over the dataset snapshot: the current target level
    # applies, and a skill removed from the role drops out of the TNI.
    rows = conn.execute(
        """
        SELECT sg.gap_id, sg.user_id, sg.role_id, sg.team_id, sg.skill_id, sg.skill,
               COALESCE(rsm.capability, sg.capability) AS capability,
               COALESCE(rsm.target_proficiency_level, sg.target_level) AS target_level,
               sg.target_level AS dataset_target, sg.current_level, sg.gap_level_formula, sg.gap_severity,
               sg.recommended_course_id, sg.tni_recommendation,
               COALESCE(rsm.is_critical, 0) AS is_critical,
               rm.role_name, rm.role_grade, rm.tower
        FROM skill_gaps sg
        LEFT JOIN role_skill_map rsm ON rsm.role_id = sg.role_id AND rsm.skill_id = sg.skill_id
        LEFT JOIN role_master rm ON rm.role_id = sg.role_id
        WHERE sg.user_id IN (SELECT value FROM json_each(?))
          AND (rsm.role_id IS NOT NULL OR NOT EXISTS (SELECT 1 FROM role_skill_map x WHERE x.role_id = sg.role_id))
        ORDER BY sg.gap_id
        """,
        (json.dumps(user_ids),),
    ).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        if row["dataset_target"] is not None and row["target_level"] != row["dataset_target"]:
            row["gap_severity"] = row["gap_level_formula"] = None  # recomputed against the new target
        out.append(row)
    return out


def _role_skill_index(conn, role_ids) -> dict[str, list]:
    by_role: dict[str, list] = {}
    for r in conn.execute(
        "SELECT role_id, skill_id, skill, capability, target_proficiency_level, is_critical FROM role_skill_map "
        "WHERE role_id IN (SELECT value FROM json_each(?)) ORDER BY role_skill_id", (json.dumps(sorted(role_ids)),),
    ).fetchall():
        by_role.setdefault(r["role_id"], []).append(r)
    return by_role


def _added_role_rows(conn, rows: list[dict]) -> list[dict]:
    """Skills added to a role after the dataset snapshot: "Not assessed" rows for its people."""
    have: dict[tuple[str, str], set[str]] = {}
    meta: dict[str, dict] = {}
    for row in rows:
        have.setdefault((row["user_id"], row["role_id"]), set()).add((row["skill_id"] or "").lower())
        meta.setdefault(row["role_id"], row)
    by_role = _role_skill_index(conn, {role_id for _, role_id in have})
    out = []
    for (user_id, role_id), skill_ids in have.items():
        role = meta[role_id]
        for r in by_role.get(role_id, []):
            if (r["skill_id"] or "").lower() in skill_ids:
                continue
            out.append({
                "gap_id": f"RSM-{user_id}-{r['skill_id']}", "user_id": user_id, "role_id": role_id, "team_id": None,
                "skill_id": r["skill_id"], "skill": r["skill"], "capability": r["capability"],
                "target_level": r["target_proficiency_level"], "current_level": None, "gap_level_formula": None,
                "gap_severity": None, "recommended_course_id": None, "tni_recommendation": None,
                "is_critical": r["is_critical"] or 0, "role_name": role["role_name"], "role_grade": role["role_grade"],
                "tower": role["tower"],
            })
    return out


def _role_rows(conn, users: list[dict]) -> list[dict]:
    """Role-skill rows (all not assessed) for employees with no Skill_Gaps_TNI rows,
    from the role their job title names."""
    roles = {r["role_name"].strip().lower(): dict(r) for r in conn.execute(
        "SELECT role_id, role_name, role_grade, tower FROM role_master").fetchall()}
    matched = {u["user_id"]: roles.get((u.get("job_title") or "").strip().lower()) for u in users}
    by_role = _role_skill_index(conn, {r["role_id"] for r in matched.values() if r})  # one query, not one per person
    out = []
    for user in users:
        role = matched[user["user_id"]]
        if not role:
            continue
        for r in by_role.get(role["role_id"], []):
            out.append({
                "gap_id": f"RSM-{user['user_id']}-{r['skill_id']}", "user_id": user["user_id"],
                "role_id": role["role_id"], "team_id": None, "skill_id": r["skill_id"], "skill": r["skill"],
                "capability": r["capability"], "target_level": r["target_proficiency_level"], "current_level": None,
                "gap_level_formula": None, "gap_severity": None, "recommended_course_id": None,
                "tni_recommendation": None, "is_critical": r["is_critical"] or 0,
                "role_name": role["role_name"], "role_grade": role["role_grade"], "tower": role["tower"],
            })
    return out


def _reviewed_levels(user_ids: list[str]) -> dict[tuple[str, str], dict]:
    """(user_id, skill) -> the newest manager-reviewed level (services/progression.py)."""
    latest: dict[tuple[str, str], dict] = {}
    for record in db.list_certifications(user_ids, basis=None):  # newest first
        latest.setdefault((record["user_id"], record["skill"].strip().lower()), record)
    return latest


def _skill_rows(conn, users: list[dict]) -> list[dict]:
    """TNI rows for these employees: dataset rows, else role rows, with manager-reviewed levels applied.

    The newest manager-reviewed record for a skill wins. A certified level never
    lowers a higher level already on file (the assessment only tested up to it);
    a confirmed gap ("assessed") sets the level the assessment showed, because
    falling short at a level contradicts any higher level on file.
    """
    ids = [u["user_id"] for u in users]
    rows = _gap_rows(conn, ids)
    rows += _added_role_rows(conn, rows)
    have = {r["user_id"] for r in rows}
    rows += _role_rows(conn, [u for u in users if u["user_id"] not in have])
    reviewed = _reviewed_levels(ids)
    for row in rows:
        row["validated"] = row["confirmed_gap"] = None
        record = reviewed.get((row["user_id"], row["skill"].strip().lower()))
        if not record:
            continue
        info = {"level": record["level"], "date": (record["created_at"] or "")[:10],
                "by": record.get("certified_by_name"), "cycle_id": record["cycle_id"]}
        if record["basis"] == "assessed":
            level = record["level"]
            row["confirmed_gap"] = info
        elif row["current_level"] is None or record["level"] >= row["current_level"]:
            level = record["level"]
            row["validated"] = info
        else:
            continue
        if level != row["current_level"]:
            row.update(current_level=level, gap_severity=None, gap_level_formula=None)
    return rows


def current_roles(users: list[dict]) -> dict[str, dict]:
    """user_id -> the role they hold today: their TNI role, else the role their job title names."""
    by_tni = db.tni_role_ids([u["user_id"] for u in users])
    roles = db.role_master_rows()
    by_id = {r["role_id"]: r for r in roles}
    by_name = {r["role_name"].strip().lower(): r for r in roles}
    out = {}
    for user in users:
        role = by_id.get(by_tni.get(user["user_id"])) or by_name.get((user.get("job_title") or "").strip().lower())
        if role:
            out[user["user_id"]] = role
    return out


def current_role(user_id: str) -> dict | None:
    user = db.get_user(user_id)
    return current_roles([user]).get(user_id) if user else None


def _weeks_for(skill: dict) -> float:
    weeks = BASE_WEEKS_PER_GAP
    if skill["severity"] == "High":
        weeks += HIGH_EXTRA_WEEKS
    if skill["is_critical"]:
        weeks += CRITICAL_EXTRA_WEEKS
    return round(weeks, 1)


def _skill_view(row: dict, courses: dict, mapped: dict) -> dict:
    assessed = row["current_level"] is not None
    current = int(round(row["current_level"])) if assessed else None
    target = int(row["target_level"] or 0)
    gap = max(target - current, 0) if assessed else None
    severity = row["gap_severity"] if assessed else None
    if assessed and not severity:
        severity = "High" if gap >= 2 else "Moderate" if gap == 1 else "No gap"
    skill = {
        "gap_id": row["gap_id"],
        "skill_id": row["skill_id"],
        "skill": row["skill"],
        "capability": row["capability"] or "—",
        "current": current,
        "target": target,
        "gap": gap,
        "assessed": assessed,
        "severity": severity or "Not assessed",
        "is_critical": bool(row["is_critical"]),
        "tni_recommendation": row["tni_recommendation"],
        "validated": row.get("validated"),
        "confirmed_gap": row.get("confirmed_gap"),
        "course": None,
        "weeks": 0.0,
    }
    if gap:
        skill["course"] = pick_course(row["skill"], row["recommended_course_id"], mapped.get(row["skill_id"], []), courses)
        skill["weeks"] = _weeks_for(skill)
    skill["status"] = (
        "Not assessed" if not assessed else "On Target" if gap == 0 else "Needs Development"
    )
    return skill


def _priority_key(skill: dict):
    # Same ordering Agent 5 uses: severity desc, critical first, gap width desc.
    return (-SEVERITY_RANK.get(skill["severity"], 0), not skill["is_critical"], -(skill["gap"] or 0), skill["gap_id"])


def _heat_cells(skill: dict) -> list[dict]:
    cells = []
    for level in LEVELS:
        if not skill["assessed"]:
            state = "unassessed"
        elif level <= skill["current"]:
            state = "achieved"
        elif level <= skill["target"]:
            state = "gap"
        else:
            state = "beyond"
        cells.append({"level": level, "state": state, "is_target": level == skill["target"]})
    return cells


def _summary(skills: list[dict]) -> dict:
    assessed = [s for s in skills if s["assessed"]]
    gaps = [s for s in assessed if s["gap"]]
    covered = [s for s in gaps if s["course"]]
    return {
        "total": len(skills),
        "assessed": len(assessed),
        "not_assessed": len(skills) - len(assessed),
        "at_target": len(assessed) - len(gaps),
        "gaps": len(gaps),
        "high": sum(1 for s in gaps if s["severity"] == "High"),
        "moderate": sum(1 for s in gaps if s["severity"] == "Moderate"),
        "readiness_pct": round(100 * (len(assessed) - len(gaps)) / len(assessed)) if assessed else 0,
        "avg_current": round(sum(s["current"] for s in assessed) / len(assessed), 1) if assessed else None,
        "avg_target": round(sum(s["target"] for s in assessed) / len(assessed), 1) if assessed else None,
        "covered": len(covered),
        "coverage_pct": round(100 * len(covered) / len(gaps)) if gaps else 100,
        "weeks_total": round(sum(s["weeks"] for s in gaps), 1),
    }


def _skill_up_path(skills: list[dict]) -> list[dict]:
    """Confirmed gaps in plan order, each with its projected start/end week (one at a time)."""
    path, week = [], 0.0
    for skill in sorted((s for s in skills if s["gap"]), key=_priority_key):
        start = week
        week = round(week + skill["weeks"], 1)
        path.append({**skill, "start_week": start, "end_week": week})
    return path


def _learning_curve(skills: list[dict], path: list[dict]) -> dict | None:
    """Projected average proficiency over time if the skill-up path is followed.

    Each gap is worked one at a time (Agent 5's "one at a time" estimate) and
    climbs one level per equal slice of its weeks, so a 3-level gap over 3
    weeks adds a level each week.
    """
    assessed = [s for s in skills if s["assessed"]]
    if not assessed:
        return None
    levels = {s["gap_id"]: s["current"] for s in assessed}
    n = len(assessed)
    points = [{"x": 0, "y": round(sum(levels.values()) / n, 2), "note": "Today"}]
    for step in path:
        slice_weeks = step["weeks"] / step["gap"]
        for i in range(1, step["gap"] + 1):
            levels[step["gap_id"]] += 1
            points.append({
                "x": round(step["start_week"] + slice_weeks * i, 2),
                "y": round(sum(levels.values()) / n, 2),
                "note": f"{step['skill']} reaches L{step['current'] + i}",
            })
    return {
        "points": points,
        "y_min": 0, "y_max": 5,
        "y_ticks": [{"v": v, "label": f"L{v}"} for v in LEVELS],
        "y_format": "level",
        "target": {"v": round(sum(s["target"] for s in assessed) / n, 2),
                   "label": f"Role target avg L{sum(s['target'] for s in assessed) / n:.1f}"},
        "x_label": "Weeks from today",
    }


# ---------------------------------------------------------------------------
# Public: one employee
# ---------------------------------------------------------------------------

def employee_tni(user_id: str) -> dict | None:
    """The employee's TNI view, or None when neither Skill_Gaps_TNI nor their job title gives a role."""
    conn = db.get_conn()
    try:
        user = conn.execute("SELECT user_id, job_title FROM users WHERE user_id = ?", (user_id,)).fetchone()
        rows = _skill_rows(conn, [dict(user)]) if user else []
        if not rows:
            return None
        courses, mapped = _catalogue(conn)
    finally:
        conn.close()
    skills = [_skill_view(r, courses, mapped) for r in rows]
    for s in skills:
        s["cells"] = _heat_cells(s)
    path = _skill_up_path(skills)
    first = rows[0]
    return {
        "user_id": user_id,
        "role_id": first["role_id"],
        "role_name": first["role_name"] or first["role_id"],
        "role_grade": first["role_grade"],
        "tower": first["tower"],
        "skills": skills,
        "summary": _summary(skills),
        "path": path,
        "uncovered": [s for s in path if not s["course"]],
        "curve": _learning_curve(skills, path),
    }


# ---------------------------------------------------------------------------
# Public: a manager's direct reports
# ---------------------------------------------------------------------------

def _team_curve(people: list[dict]) -> dict | None:
    """Projected share of the group's assessed skills at target, week by week."""
    events, assessed_total, at_target = [], 0, 0
    for person in people:
        assessed_total += person["summary"]["assessed"]
        at_target += person["summary"]["at_target"]
        events += [step["end_week"] for step in person["path"]]
    if not assessed_total:
        return None
    points = [{"x": 0, "y": round(100 * at_target / assessed_total, 1), "note": "Today"}]
    for week in sorted(set(events)):
        closed = events.count(week)
        at_target += closed
        points.append({"x": week, "y": round(100 * at_target / assessed_total, 1),
                       "note": f"{closed} gap{'s' if closed != 1 else ''} closed"})
    return {
        "points": points, "y_min": 0, "y_max": 100,
        "y_ticks": [{"v": v, "label": f"{v}%"} for v in (0, 25, 50, 75, 100)],
        "y_format": "percent", "target": {"v": 100, "label": "All skills at target"},
        "x_label": "Weeks from today",
    }


def _plans(conn, users: list[dict]) -> dict[str, dict]:
    """user_id -> TNI view (skills, path, summary, role) for many employees from one connection."""
    rows = _skill_rows(conn, users)
    courses, mapped = _catalogue(conn)
    by_user: dict[str, list[dict]] = {}
    for row in rows:
        by_user.setdefault(row["user_id"], []).append(row)
    plans = {}
    for user in users:
        user_rows = by_user.get(user["user_id"])
        if not user_rows:
            continue
        skills = [_skill_view(r, courses, mapped) for r in user_rows]
        plans[user["user_id"]] = {
            "user_id": user["user_id"], "role_id": user_rows[0]["role_id"],
            "role_name": user_rows[0]["role_name"] or user_rows[0]["role_id"], "role_grade": user_rows[0]["role_grade"],
            "tower": user_rows[0]["tower"], "skills": skills, "path": _skill_up_path(skills), "summary": _summary(skills),
        }
    return plans


def plans_for(users: list[dict]) -> dict[str, dict]:
    """Bulk TNI views (no learning curve) -- used by the status engine to score thousands of people per batch."""
    conn = db.get_conn()
    try:
        return _plans(conn, users)
    finally:
        conn.close()


def _people(manager_id: str | None = None) -> tuple[list[dict], list[dict]]:
    """(reports, people): employees (one manager's direct reports, or everyone) and the
    TNI view of each one with a known role."""
    conn = db.get_conn()
    try:
        reports = [dict(r) for r in conn.execute(
            "SELECT user_id, full_name, email, job_title, manager_id FROM users "
            "WHERE role = 'employee' AND (? IS NULL OR manager_id = ?) ORDER BY user_id",
            (manager_id, manager_id),
        ).fetchall()]
        plans = _plans(conn, reports)
    finally:
        conn.close()

    people = []
    for report in reports:
        plan = plans.get(report["user_id"])
        if not plan:
            continue
        people.append({
            "user_id": report["user_id"], "name": report["full_name"], "email": report["email"],
            "manager_id": report["manager_id"], "role_id": plan["role_id"], "role_name": plan["role_name"],
            "tower": plan["tower"], "skills": plan["skills"], "path": plan["path"], "summary": plan["summary"],
        })
    return reports, people


def org_tni() -> dict:
    """Every employee's TNI view, for the leader's organisation report."""
    reports, people = _people()
    return {"reports": reports, "people": people,
            "summary": _summary([s for p in people for s in p["skills"]]) if people else None}


def summarise(skills: list[dict]) -> dict:
    return _summary(skills)


def team_tni(manager_id: str, role_id: str | None = None) -> dict:
    """TNI for a manager's direct reports; heatmap/curve/courses scoped to one role."""
    reports, people = _people(manager_id)

    roles: dict[str, dict] = {}
    for person in people:
        role = roles.setdefault(person["role_id"], {"role_id": person["role_id"], "role_name": person["role_name"], "people": []})
        role["people"].append(person)
    role_rows = []
    for role in roles.values():
        summary = _summary([s for p in role["people"] for s in p["skills"]])
        role_rows.append({"role_id": role["role_id"], "role_name": role["role_name"],
                          "employees": len(role["people"]), **summary})

    selected = role_id if role_id in roles else (role_rows[0]["role_id"] if role_rows else None)
    group = roles[selected]["people"] if selected else []

    # Heatmap columns: the role's skills in dataset order.
    columns: list[str] = []
    for person in group:
        for s in person["skills"]:
            if s["skill"] not in columns:
                columns.append(s["skill"])
    heat_rows = []
    for person in group:
        by_skill = {s["skill"]: s for s in person["skills"]}
        heat_rows.append({"user_id": person["user_id"], "name": person["name"],
                          "readiness_pct": person["summary"]["readiness_pct"],
                          "cells": [by_skill.get(skill) for skill in columns]})

    # Recommended learning for the selected role, grouped by course.
    course_rows: dict[str, dict] = {}
    uncovered = []
    for person in group:
        for step in person["path"]:
            if not step["course"]:
                uncovered.append({"name": person["name"], **step})
                continue
            course = course_rows.setdefault(step["course"]["course_id"], {
                **step["course"], "skills": [], "employees": [], "high": 0,
            })
            if step["skill"] not in course["skills"]:
                course["skills"].append(step["skill"])
            course["employees"].append({"user_id": person["user_id"], "name": person["name"],
                                        "current": step["current"], "target": step["target"]})
            course["high"] += step["severity"] == "High"

    all_skills = [s for p in people for s in p["skills"]]
    return {
        "members": len(reports),
        "reports": reports,
        "people": people,
        "summary": _summary(all_skills) if all_skills else None,
        "roles": role_rows,
        "selected_role": selected,
        "selected_role_name": roles[selected]["role_name"] if selected else None,
        "role_summary": _summary([s for p in group for s in p["skills"]]) if group else None,
        "heatmap": {"columns": columns, "rows": heat_rows},
        "courses": sorted(course_rows.values(), key=lambda c: (-len(c["employees"]), c["course_id"])),
        "uncovered": uncovered,
        "curve": _team_curve(group),
        "role_weeks": max((p["summary"]["weeks_total"] for p in group), default=0),
    }



def get_course(course_id: str) -> dict | None:
    """One training_catalogue course with the skills it is mapped to, for the course detail page."""
    conn = db.get_conn()
    try:
        course = conn.execute(
            "SELECT course_id, tower, course_title, delivery_type, level_group, risk_level, spoc_role "
            "FROM training_catalogue WHERE course_id = ?", (course_id,),
        ).fetchone()
        if not course or not _usable(course["course_title"]):
            return None
        # training_skill_map.skill is blank on some R2R rows; fall back to the skill tables.
        skills = sorted({r["skill"] for r in conn.execute(
            """
            SELECT COALESCE(tsm.skill, sm.skill,
                            (SELECT sg.skill FROM skill_gaps sg WHERE sg.skill_id = tsm.skill_id LIMIT 1)) AS skill
            FROM training_skill_map tsm LEFT JOIN skill_master sm ON sm.skill_id = tsm.skill_id
            WHERE tsm.course_id = ?
            """, (course_id,),
        ).fetchall() if r["skill"]})
    finally:
        conn.close()
    return {**dict(course), "skills": skills}
