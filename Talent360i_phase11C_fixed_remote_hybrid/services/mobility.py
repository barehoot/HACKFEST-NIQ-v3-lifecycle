"""Career mobility: aspirations across departments and verticals, cross-skilling, internal
applications and the talent pool hiring managers search.

  aspirations     an employee picks up to five target roles -- any job profile in any vertical,
                  or an internal posting (SmartRecruiters) -- with a note on why
  levels          what we know about each skill, strongest source first: validated by a manager
                  after an assessment, assessed in the current role (TNI), then claimed on the CV
                  (Agent 8 quoted the CV; only the claim and the quote are kept)
  gap analysis    the target role's skills against those levels: readiness, critical gaps, the
                  catalogue course for each gap, a one-gap-at-a-time skill-up path and its
                  projected learning curve. Promotion readiness (Ready now / 6-12 months / Not
                  yet / On hold) combines the assessed levels -- never CV claims -- with performance
  upskilling      gap courses booked on the employee's calendar (and exported with it)
  applications    an employee applies for a role or posting; the line manager and the hiring
                  contact are told; a hiring manager shortlists or declines with a note
  talent pool     for a role: the people who aspire to it or applied, ranked by readiness, the
                  viewer's own team first; optionally others whose recorded skills fit
                  ("potential"), shown without performance data unless the viewer already has
                  scope over them

Nothing here changes a job record. A move still goes through the nomination (manager ->
leader) or the hiring process.
"""
from __future__ import annotations

from datetime import date, timedelta

from domain.governance import sanitise_text
from services import db, engagement, lifecycle_store as store, performance, progression, schedule, tni

MAX_ASPIRATIONS = 5
MAX_UPSKILL_SESSIONS = 8
POTENTIAL_LIMIT = 200
SOURCE_LABELS = {"validated": "Validated", "assessed": "Assessed", "cv": "CV claim"}
POOL_VIEWERS = ("manager", "leader", "hr", "admin")


class MobilityError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Roles and levels
# ---------------------------------------------------------------------------

def role_options() -> list[dict]:
    """Every job profile, with its vertical and department, for the aspiration picker."""
    meta = store.job_profiles()
    verticals = {v["code"]: v["name"] for v in store.list_verticals()}
    return [{**r, "vertical": verticals.get(r["tower"], r["tower"]),
             "department": (meta.get(r["role_id"]) or {}).get("department_name")} for r in db.role_master_rows()]


def levels_for(user_id: str, *, plan: dict | None = None, certs: list[dict] | None = None,
               claims: list[dict] | None = None) -> dict[str, dict]:
    """skill (lower) -> {"level", "validated", "source", "cv_level"?, "quote"?}."""
    plan = plan if plan is not None else tni.employee_tni(user_id)
    certs = certs if certs is not None else db.list_certifications([user_id], basis=None)
    claims = claims if claims is not None else store.cv_claims([user_id]).get(user_id, [])
    out = {k: {**v, "source": "validated" if v["validated"] else "assessed"}
           for k, v in progression._levels_from(plan, certs).items()}
    for claim in claims:
        key = claim["skill"].strip().lower()
        if key in out:  # an assessed level always wins; the claim is shown beside it
            out[key].update(cv_level=claim["level"], quote=claim.get("quote"))
        else:
            out[key] = {"level": claim["level"], "validated": False, "source": "cv", "cv_level": claim["level"],
                        "quote": claim.get("quote")}
    return out


def _assessed_only(levels: dict[str, dict]) -> dict[str, dict]:
    return {k: v for k, v in levels.items() if v["source"] != "cv"}


def _courses(skills: list[dict]) -> dict[str, dict]:
    """skill (lower) -> the best catalogue course for it, mapped by skill id or by name."""
    conn = db.get_conn()
    try:
        courses, mapped = tni._catalogue(conn)
        by_name: dict[str, list[str]] = {}
        for row in conn.execute("SELECT lower(skill), course_id FROM training_skill_map ORDER BY training_skill_map_id"):
            by_name.setdefault(row[0], []).append(row[1])
    finally:
        conn.close()
    out = {}
    for s in skills:
        ids = list(dict.fromkeys(mapped.get(s.get("skill_id"), []) + by_name.get(s["skill"].strip().lower(), [])))
        course = tni.pick_course(s["skill"], None, ids, courses)
        if course:
            out[s["skill"].strip().lower()] = course
    return out


def _weeks(gap: int, critical: bool) -> float:
    # Agent 5's duration model, as TNI uses it: one gap at a time.
    weeks = tni.BASE_WEEKS_PER_GAP + (tni.HIGH_EXTRA_WEEKS if gap >= 2 else 0) + (tni.CRITICAL_EXTRA_WEEKS if critical else 0)
    return round(weeks, 1)


def _curve(rows: list[dict], path: list[dict]) -> dict | None:
    """Projected average level across the target role's skills if the path is followed
    (a skill not assessed yet starts at L0)."""
    if not rows:
        return None
    levels = {r["skill"]: r["level"] or 0 for r in rows}
    n = len(rows)
    points = [{"x": 0, "y": round(sum(levels.values()) / n, 2), "note": "Today"}]
    for step in path:
        slice_weeks = step["weeks"] / step["gap_to_close"]
        start_level = levels[step["skill"]]
        for i in range(1, step["gap_to_close"] + 1):
            levels[step["skill"]] = start_level + i
            points.append({"x": round(step["start_week"] + slice_weeks * i, 2), "y": round(sum(levels.values()) / n, 2),
                           "note": f"{step['skill']} reaches L{start_level + i}"})
    target = round(sum(r["target"] for r in rows) / n, 2)
    return {"points": points, "y_min": 0, "y_max": 5, "y_ticks": [{"v": v, "label": f"L{v}"} for v in tni.LEVELS],
            "y_format": "level", "target": {"v": target, "label": f"Target avg L{target:.1f}"}, "x_label": "Weeks from today"}


_UNSET = object()


def gap_analysis(user_id: str, role_id: str, *, levels: dict | None = None, current: dict | None = None,
                 status_row: dict | None = None, courses: dict | None = None, skills: list[dict] | None = None,
                 review=_UNSET, escalations: list[dict] | None = None, current_dept=_UNSET, target_dept=_UNSET) -> dict | None:
    """The bulk callers (talent pool) preload review / escalations / departments; one-off callers don't."""
    target = progression.role_by_id(role_id)
    if not target:
        return None
    skills = skills if skills is not None else db.role_skill_details(role_id)
    levels = levels if levels is not None else levels_for(user_id)
    current = current if current is not None else tni.current_role(user_id)
    view = progression.readiness_from(user_id, current, target, levels, skills)            # incl. CV claims
    assessed = progression.readiness_from(user_id, current, target, _assessed_only(levels), skills)
    courses = courses if courses is not None else _courses(skills)
    rows = []
    for row in view["rows"]:
        known = levels.get(row["skill"].strip().lower(), {})
        gap = max(row["target"] - (row["level"] or 0), 0)
        rows.append({**row, "source": known.get("source") if row["level"] is not None else None,
                     "source_label": SOURCE_LABELS.get(known.get("source"), "Not assessed") if row["level"] is not None else "Not assessed",
                     "quote": known.get("quote"), "cv_level": known.get("cv_level"), "gap_to_close": gap,
                     "course": courses.get(row["skill"].strip().lower()) if gap else None,
                     "weeks": _weeks(gap, row["is_critical"]) if gap else 0})
    path, week = [], 0.0
    for row in sorted((r for r in rows if r["gap_to_close"]), key=lambda r: (not r["is_critical"], -r["gap_to_close"], r["skill"])):
        start, week = week, round(week + row["weeks"], 1)
        path.append({**row, "start_week": start, "end_week": week})
    status_row = status_row if status_row is not None else store.status_rows([user_id]).get(user_id)
    band = (status_row or {}).get("band") or "Not enough data"
    review = store.latest_reviews([user_id]).get(user_id) if review is _UNSET else review
    escalations = escalations if escalations is not None else store.list_escalations(user_ids=[user_id])
    promotion = performance.promotion_level(assessed, band, review, [e for e in escalations if e["status"] != "resolved"])
    current_dept = (store.get_profile(user_id) or {}).get("dept_id") if current_dept is _UNSET else current_dept
    target_dept = (store.job_profiles().get(role_id) or {}).get("dept_id") if target_dept is _UNSET else target_dept
    return {
        "role": target, "current": current, "rows": rows, "path": path, "curve": _curve(rows, path),
        "weeks_total": week, "pct": view["pct"], "band": view["band"], "met": view.get("met", 0), "total": view.get("total", 0),
        "critical_missed": view["critical_missed"], "unknown": view["unknown"],
        "assessed_pct": assessed["pct"], "assessed_band": assessed["band"],
        "cv_only": sum(1 for r in rows if r["met"] and r["source"] == "cv"),
        "uncovered": [r["skill"] for r in path if not r["course"]], "promotion": promotion, "band_now": band,
        "cross_vertical": bool(current and current.get("tower") != target["tower"]),
        "cross_department": bool(target_dept and current_dept != target_dept),
    }


def fit_for_roles(user_id: str, role_ids) -> dict[str, dict]:
    """role_id -> gap analysis for one employee, loading their levels once (the postings list)."""
    role_ids = [r for r in dict.fromkeys(role_ids) if r]
    if not role_ids:
        return {}
    levels, current = levels_for(user_id), tni.current_role(user_id)
    status_row = store.status_rows([user_id]).get(user_id) or {}
    out = {}
    for role_id in role_ids:
        analysis = gap_analysis(user_id, role_id, levels=levels, current=current, status_row=status_row)
        if analysis:
            out[role_id] = analysis
    return out


# ---------------------------------------------------------------------------
# Aspirations
# ---------------------------------------------------------------------------

def aspirations(user_id: str) -> list[dict]:
    rows = store.list_aspirations(user_id=user_id)
    if not rows:
        return []
    levels = levels_for(user_id)
    current = tni.current_role(user_id)
    status_row = store.status_rows([user_id]).get(user_id)
    for row in rows:
        role_id = row["role_id"]
        if not role_id and row["posting_id"]:  # a posting HR has since mapped to a job profile
            role_id = (store.get_posting(row["posting_id"]) or {}).get("role_id")
        row["analysis"] = gap_analysis(user_id, role_id, levels=levels, current=current, status_row=status_row) if role_id else None
        row["target_role_id"] = role_id
        row["application"] = next((a for a in store.list_applications(user_id=user_id, statuses=("submitted", "shortlisted"))
                                   if (role_id and a["role_id"] == role_id) or (row["posting_id"] and a["posting_id"] == row["posting_id"])), None)
    return rows


def add_aspiration(user_id: str, *, role_id: str = "", posting_id: str = "", note: str = "") -> str:
    role_id, posting_id = sanitise_text(role_id, 120), sanitise_text(posting_id, 80)
    posting = store.get_posting(posting_id) if posting_id else None
    if posting_id and not posting:
        raise MobilityError("That internal posting is no longer open.")
    if role_id and not progression.role_by_id(role_id):
        raise MobilityError("Choose a job profile.")
    if not role_id and not posting:
        raise MobilityError("Choose a job profile or an internal posting.")
    role_id = role_id or (posting or {}).get("role_id") or None
    current = tni.current_role(user_id)
    if role_id and current and current["role_id"] == role_id:
        raise MobilityError("That is the role you hold today.")
    active = store.list_aspirations(user_id=user_id)
    if len(active) >= MAX_ASPIRATIONS:
        raise MobilityError(f"You can follow up to {MAX_ASPIRATIONS} aspirations. Withdraw one first.")
    if any((role_id and a["role_id"] == role_id) or (posting_id and a["posting_id"] == posting_id) for a in active):
        raise MobilityError("You already aspire to that role.")
    return store.add_aspiration(user_id=user_id, role_id=role_id, posting_id=posting_id or None,
                                note=sanitise_text(note, 400))


def cv_targets(user_id: str) -> list[str]:
    """The skills worth looking for in this employee's CV: their role's and their aspirations'."""
    names: list[str] = []
    current = tni.current_role(user_id)
    role_ids = ([current["role_id"]] if current else []) + [a["role_id"] for a in store.list_aspirations(user_id=user_id) if a["role_id"]]
    for role_id in role_ids:
        names += [s["skill"] for s in db.role_skill_details(role_id)]
    return list(dict.fromkeys(names))[:40]


def save_cv_claims(user_id: str, result: dict, filename: str) -> int:
    allowed = {s.lower(): s for s in cv_targets(user_id)}
    claims = []
    for claim in result.get("claims") or []:
        if not isinstance(claim, dict) or not isinstance(claim.get("skill"), str):
            continue
        name = allowed.get(claim["skill"].strip().lower())
        level = claim.get("level")
        if name and isinstance(level, int) and 1 <= level <= 5:
            claims.append({"skill": name, "level": level, "quote": sanitise_text(claim.get("quote"), 160)})
    generated_by = result.get("generated_by") if result.get("generated_by") in ("agent", "rules") else "rules"
    return store.replace_cv_claims(user_id, claims, generated_by, sanitise_text(filename, 120))


# ---------------------------------------------------------------------------
# Upskilling: gap courses onto the calendar
# ---------------------------------------------------------------------------

def book_upskilling(user_id: str, role_id: str, *, start: date, cadence_days: int, by: str,
                    start_time: str = "10:00", duration: int = 60) -> dict:
    analysis = gap_analysis(user_id, role_id)
    if not analysis:
        raise MobilityError("Unknown target role.")
    booked = db.scheduled_course_ids(user_id)
    created, skipped, day = 0, 0, schedule.add_business_days(start, 0)
    for step in analysis["path"]:
        course = step["course"]
        if not course:
            continue
        if course["course_id"] in booked:
            skipped += 1
            continue
        if created >= MAX_UPSKILL_SESSIONS:
            break
        db.create_event(event_type="training", title=course["course_title"], event_date=day.isoformat(), start_time=start_time,
                        duration_minutes=duration, user_id=user_id, role_id=role_id, skill=step["skill"],
                        course_id=course["course_id"], created_by=by,
                        notes=(f"Upskilling for {analysis['role']['role_name']} · L{step['level'] or 0}→L{step['target']}"
                               f" · est. {step['weeks']} wks to close"))
        booked.add(course["course_id"])
        created += 1
        day = schedule.add_business_days(day + timedelta(days=cadence_days), 0)
    if created:
        manager = engagement.manager_of(user_id)
        name = (db.get_user(user_id) or {}).get("full_name", user_id)
        engagement.notify([manager] if manager else [], "development",
                          f"{name} booked {created} upskilling session(s) toward {analysis['role']['role_name']}",
                          "A career aspiration they are working on.", f"/people/{user_id}#career")
    return {"created": created, "skipped": skipped, "uncovered": analysis["uncovered"], "role": analysis["role"]}


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

def _hiring_contacts(role_id: str | None, posting: dict | None) -> list[str]:
    if posting and posting.get("hiring_manager_id"):
        return [posting["hiring_manager_id"]]
    dept_id = (store.job_profiles().get(role_id or "") or {}).get("dept_id")
    department = store.get_department(dept_id) if dept_id else None
    if department and department.get("leader_id"):
        return [department["leader_id"]]
    return store.user_ids_with_role("hr")


def apply(user_id: str, *, role_id: str = "", posting_id: str = "", note: str = "") -> str:
    role_id, posting_id = sanitise_text(role_id, 120), sanitise_text(posting_id, 80)
    posting = store.get_posting(posting_id) if posting_id else None
    if posting_id and (not posting or posting["status"] != "open"):
        raise MobilityError("That internal posting is no longer open.")
    role_id = role_id or (posting or {}).get("role_id") or ""
    if role_id and not progression.role_by_id(role_id):
        raise MobilityError("Unknown job profile.")
    if not role_id and not posting:
        raise MobilityError("Choose a job profile or an internal posting.")
    note = sanitise_text(note, 1500)
    if len(note) < 20:
        raise MobilityError("Tell the hiring team why you want this role (a couple of sentences).")
    analysis = gap_analysis(user_id, role_id) if role_id else None
    snapshot = {k: analysis[k] for k in ("pct", "band", "assessed_pct", "critical_missed", "cv_only")} if analysis else {}
    if analysis:
        snapshot["promotion"] = analysis["promotion"]["level"]
    application_id = store.add_application(user_id=user_id, role_id=role_id or None, posting_id=posting_id or None,
                                           note=note, readiness=snapshot)
    if not application_id:
        raise MobilityError("You already have an open application for this role.")
    if not any((role_id and a["role_id"] == role_id) or (posting_id and a["posting_id"] == posting_id)
               for a in store.list_aspirations(user_id=user_id)) and len(store.list_aspirations(user_id=user_id)) < MAX_ASPIRATIONS:
        store.add_aspiration(user_id=user_id, role_id=role_id or None, posting_id=posting_id or None, note="Applied")
    name = (db.get_user(user_id) or {}).get("full_name", user_id)
    title = (analysis or {}).get("role", {}).get("role_name") or (posting or {}).get("title")
    readiness = f" Readiness {snapshot['pct']}% ({snapshot.get('promotion')})." if snapshot else ""
    link = f"/talent?role={role_id}" if role_id else "/talent"
    engagement.notify(_hiring_contacts(role_id or None, posting), "mobility", f"Internal application: {name} for {title}",
                      note[:200] + readiness, link)
    manager = engagement.manager_of(user_id)
    if manager:
        engagement.notify([manager], "mobility", f"{name} applied internally for {title}",
                          "Internal mobility is open to everyone; you can add an endorsement on their profile.", f"/people/{user_id}#career")
    return application_id


def decide_application(viewer: dict, application_id: str, decision: str, note: str) -> dict:
    if viewer["role"] not in POOL_VIEWERS:
        raise PermissionError("Access restricted")
    application = store.get_application(sanitise_text(application_id, 40))
    if not application:
        raise MobilityError("Application not found.")
    if application["user_id"] == viewer["id"]:
        raise PermissionError("Access restricted")
    note = sanitise_text(note, 600)
    if decision not in ("shortlisted", "declined"):
        raise MobilityError("Shortlist or decline the application.")
    if decision == "declined" and len(note) < 10:
        raise MobilityError("Tell the applicant why, so they know what to work on.")
    if not store.set_application_status(application["application_id"], decision, viewer["id"], note):
        raise MobilityError("This application has already been decided.")
    role = progression.role_by_id(application["role_id"]) or {}
    title = role.get("role_name") or (store.get_posting(application["posting_id"] or "") or {}).get("title") or "the role"
    engagement.notify([application["user_id"]], "mobility",
                      f"Your application for {title} was {'shortlisted' if decision == 'shortlisted' else 'not taken forward'}",
                      note or "The hiring manager will be in touch.", "/employee/career#applications")
    return application


def withdraw_application(user_id: str, application_id: str) -> bool:
    application = store.get_application(sanitise_text(application_id, 40))
    if not application or application["user_id"] != user_id:
        return False
    return store.set_application_status(application["application_id"], "withdrawn", user_id, "Withdrawn by the applicant.")


# ---------------------------------------------------------------------------
# Talent pool
# ---------------------------------------------------------------------------

def _viewer_scope(viewer: dict) -> set[str] | None:
    """Employees whose performance data the viewer may already see (None: everyone)."""
    if viewer["role"] in ("hr", "admin"):
        return None
    if viewer["role"] == "manager":
        return set(store.employee_ids(viewer["id"]))
    depts = store.departments_led_by(viewer["id"])
    return None if not depts else store.status_ids({"dept_ids": depts})


def in_scope(viewer: dict, user_id: str) -> bool:
    """Whether the viewer can already open this employee's 360 profile."""
    scope = _viewer_scope(viewer)
    return scope is None or user_id in scope


def talent_pool(role_id: str, viewer: dict, *, include_potential: bool = False) -> dict | None:
    if viewer["role"] not in POOL_VIEWERS:
        raise PermissionError("Access restricted")
    role = progression.role_by_id(role_id)
    if not role:
        return None
    skills = db.role_skill_details(role_id)
    aspiring = {a["user_id"]: a for a in store.list_aspirations(role_ids=[role_id])}
    applied = {}
    for a in store.list_applications(role_id=role_id, statuses=("submitted", "shortlisted", "declined")):
        applied.setdefault(a["user_id"], a)
    ids = list(dict.fromkeys([*aspiring, *applied]))
    potential: set[str] = set()
    need = max(1, len(skills) // 3)  # a "potential" candidate already meets a third of the role's targets
    if include_potential:
        found = store.users_with_skills([s["skill"] for s in skills], role["role_name"], POTENTIAL_LIMIT)
        potential = {uid for uid in found if uid not in ids}
        ids += [uid for uid in found if uid in potential]
    ids = [uid for uid in ids if uid != viewer["id"]]
    users = [u for u in store.users_by_ids(ids) if u["role"] == "employee"]
    plans = tni.plans_for(users)
    certs: dict[str, list[dict]] = {}
    for record in db.list_certifications([u["user_id"] for u in users], basis=None):
        certs.setdefault(record["user_id"], []).append(record)
    claims = store.cv_claims([u["user_id"] for u in users])
    user_ids = [u["user_id"] for u in users]
    statuses = store.status_rows(user_ids)
    reviews, profiles = store.latest_reviews(user_ids), store.profiles_for(user_ids)
    escalations: dict[str, list[dict]] = {}
    for e in store.list_escalations(user_ids=user_ids):
        escalations.setdefault(e["user_id"], []).append(e)
    target_dept = (store.job_profiles().get(role_id) or {}).get("dept_id")
    courses = _courses(skills)
    scope = _viewer_scope(viewer)
    shortlisted = store.shortlists(role_id)
    team, elsewhere = [], []
    for user in users:
        uid = user["user_id"]
        plan = plans.get(uid)
        current = {k: plan[k] for k in ("role_id", "role_name", "role_grade", "tower")} if plan else None
        levels = levels_for(uid, plan=plan, certs=certs.get(uid, []), claims=claims.get(uid, []))
        analysis = gap_analysis(uid, role_id, levels=levels, current=current, status_row=statuses.get(uid) or {},
                                courses=courses, skills=skills, review=reviews.get(uid), escalations=escalations.get(uid, []),
                                current_dept=(profiles.get(uid) or {}).get("dept_id"), target_dept=target_dept)
        if uid in potential and analysis["met"] < need:
            continue
        opted_in = uid in aspiring or uid in applied
        status_row = statuses.get(uid) or {}
        show_performance = opted_in or scope is None or uid in scope
        row = {"user_id": uid, "name": user["full_name"], "job_title": user["job_title"],
               "department": status_row.get("department"), "manager_id": user["manager_id"],
               "aspiration": aspiring.get(uid), "application": applied.get(uid), "opted_in": opted_in,
               "potential": uid in potential, "analysis": analysis, "show_performance": show_performance,
               "band": status_row.get("band") if show_performance else None,
               "tier": performance.TIERS.get(status_row.get("band")) if show_performance else None,
               "tone": performance.BAND_TONE.get(status_row.get("band"), "none") if show_performance else "none",
               "promotion": analysis["promotion"]["level"] if show_performance else None,
               "shortlisted_by": [s["added_by_name"] for s in shortlisted.get(uid, [])],
               "shortlisted_by_me": any(s["added_by"] == viewer["id"] for s in shortlisted.get(uid, []))}
        (team if viewer["role"] == "manager" and user["manager_id"] == viewer["id"] else elsewhere).append(row)
    order = {"Ready now": 0, "Ready in 6-12 months": 1, "Not yet": 2, "On hold": 3, None: 4}

    def rank(r):
        return (not r["application"], not r["opted_in"], order.get(r["promotion"], 4), -(r["analysis"]["assessed_pct"] or 0),
                -(r["analysis"]["pct"] or 0), len(r["analysis"]["critical_missed"]), r["name"])
    team.sort(key=rank)
    elsewhere.sort(key=rank)
    return {"role": role, "skills": skills, "team": team, "elsewhere": elsewhere,
            "postings": store.list_postings(role_id=role_id), "include_potential": include_potential,
            "counts": {"aspiring": len(aspiring), "applied": len(applied), "potential": len(potential)}}


def talent_card(role_id: str, user_id: str, viewer: dict) -> dict | None:
    """One candidate as the pool shows them to this viewer (None when they would not be in it)."""
    pool = talent_pool(role_id, viewer, include_potential=True)
    if not pool:
        return None
    row = next((r for r in pool["team"] + pool["elsewhere"] if r["user_id"] == user_id), None)
    if not row:
        return None
    row["cv_claims"] = store.cv_claims([user_id]).get(user_id, []) if row["opted_in"] else []
    return {"role": pool["role"], "candidate": row}


def shortlist(viewer: dict, role_id: str, user_id: str, note: str) -> bool:
    card = talent_card(role_id, user_id, viewer)
    if not card:
        raise MobilityError("That person is not in the talent pool for this role.")
    note = sanitise_text(note, 600)
    if not store.add_shortlist(role_id=role_id, user_id=user_id, added_by=viewer["id"], note=note):
        raise MobilityError("You have already shortlisted this person for the role.")
    application = card["candidate"]["application"]
    if application and application["status"] == "submitted":
        store.set_application_status(application["application_id"], "shortlisted", viewer["id"], note or "Shortlisted.")
    engagement.notify([user_id], "mobility", f"You were shortlisted for {card['role']['role_name']}",
                      note or f"{viewer['name']} would like to talk to you about the role.", "/employee/career")
    return True


def team_aspirations(user_ids: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for a in store.list_aspirations():
        if a["user_id"] in user_ids:
            out.setdefault(a["user_id"], []).append(a)
    return out


def roles_in_demand() -> list[dict]:
    """Job profiles with aspirants, applicants or open postings, busiest first: the pool's role picker."""
    counts: dict[str, dict] = {}
    for a in store.list_aspirations():
        if a["role_id"]:
            counts.setdefault(a["role_id"], {"aspiring": 0, "applied": 0, "postings": 0})["aspiring"] += 1
    for a in store.list_applications(statuses=("submitted", "shortlisted")):
        if a["role_id"]:
            counts.setdefault(a["role_id"], {"aspiring": 0, "applied": 0, "postings": 0})["applied"] += 1
    for p in store.list_postings():
        if p["role_id"]:
            counts.setdefault(p["role_id"], {"aspiring": 0, "applied": 0, "postings": 0})["postings"] += 1
    out = [{**r, **counts.get(r["role_id"], {"aspiring": 0, "applied": 0, "postings": 0})} for r in role_options()]
    return sorted(out, key=lambda r: (-(r["applied"] * 3 + r["aspiring"] * 2 + r["postings"]), r["tower"], r["role_name"]))

