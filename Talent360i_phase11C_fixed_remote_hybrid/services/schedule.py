"""Scheduler calendar: assessments and training booked per employee, plus the
live resume-driven assessment cycles, laid out as a month grid and an agenda.

Bookings persist in scheduled_events (services/db.py). Recommended training
comes from the employee's TNI skill-up path (services/tni.py), so a course is
only ever booked against a real skill gap and a real catalogue course.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta, timezone

from services import db, tni

EVENT_KINDS = {
    "assessment": "Assessment",
    "training": "Training",
    "activity": "Assessment activity",
}
MAX_CHIPS_PER_DAY = 3
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

CYCLE_ACTIVITY = {
    "resume_uploaded": "Resume uploaded",
    "pending_sme_setup": "Resume received · SME setting up assessment",
    "pending_sme_approval": "Questions awaiting SME approval",
    "ready_for_assessment": "Assessment ready to take",
    "mcq_submitted": "MCQ submitted · case study next",
    "case_study_submitted": "Case study submitted",
    "pending_manager_approval": "Awaiting manager review",
    "manager_approved": "Results approved · plan ready",
    "manager_rejected": "Manager review completed",
}


# ---------------------------------------------------------------------------
# Input parsing (all raise ValueError with a user-safe message)
# ---------------------------------------------------------------------------

def parse_date(value: str | None) -> date:
    try:
        return date.fromisoformat((value or "").strip()[:10])
    except ValueError:
        raise ValueError("Enter a valid date.") from None


def parse_time(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if not _TIME_RE.match(value):
        raise ValueError("Enter a valid time (HH:MM).")
    return value


def parse_month(value: str | None, today: date) -> date:
    """'2026-09' -> date(2026, 9, 1); anything else falls back to the current month."""
    match = re.match(r"^(\d{4})-(\d{2})$", (value or "").strip())
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        if 2000 <= year <= 2100 and 1 <= month <= 12:
            return date(year, month, 1)
    return today.replace(day=1)


def add_business_days(day: date, count: int) -> date:
    while day.weekday() >= 5:
        day += timedelta(days=1)
    for _ in range(count):
        day += timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
    return day


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _event_item(row: dict, today: date) -> dict:
    who = row.get("employee_name") or ("Cohort" if row["event_type"] == "assessment" else "—")
    detail = row.get("skill") or row.get("role_id") or ""
    return {
        "id": row["event_id"],
        "kind": row["event_type"],
        "kind_label": EVENT_KINDS[row["event_type"]],
        "date": row["event_date"],
        "time": row.get("start_time"),
        "duration": row.get("duration_minutes"),
        "title": row["title"],
        "short_title": tni.short_course_title(row["title"]),
        "who": who,
        "user_id": row.get("user_id"),
        "detail": detail,
        "course_id": row.get("course_id"),
        "notes": row.get("notes"),
        "status": row["status"],
        "can_cancel": row["status"] == "scheduled" and row["event_date"] >= today.isoformat(),
    }


def _activity_item(row: dict) -> dict:
    label = CYCLE_ACTIVITY.get(row["status"], row["status"].replace("_", " ").title())
    return {
        "id": row["cycle_id"],
        "kind": "activity",
        "kind_label": EVENT_KINDS["activity"],
        "date": row["updated_at"][:10],
        "time": row["updated_at"][11:16] or None,
        "duration": None,
        "title": label,
        "short_title": label,
        "who": row["employee_name"],
        "user_id": row["user_id"],
        "detail": row.get("matched_role_name") or "",
        "course_id": None,
        "notes": row["cycle_id"],
        "status": row["status"],
        "can_cancel": False,
    }


def _items(start: date, end: date, today: date, kind: str = "all", user_id: str | None = None) -> list[dict]:
    items = [_event_item(r, today) for r in db.list_events(start.isoformat(), end.isoformat(), user_id=user_id)]
    if kind in ("all", "assessment"):
        items += [_activity_item(r) for r in db.list_cycle_activity(start.isoformat(), end.isoformat(), user_id)]
    if kind in ("assessment", "training"):
        items = [i for i in items if i["kind"] == kind or (kind == "assessment" and i["kind"] == "activity")]
    return sorted(items, key=lambda i: (i["date"], i["time"] or "", i["kind"], i["who"]))


def month_view(month_start: date, today: date, kind: str = "all", user_id: str | None = None) -> dict:
    kind = kind if kind in ("all", "assessment", "training") else "all"
    _, days_in_month = calendar.monthrange(month_start.year, month_start.month)
    month_end = month_start.replace(day=days_in_month)
    grid_start = month_start - timedelta(days=month_start.weekday())
    grid_end = month_end + timedelta(days=6 - month_end.weekday())

    items = _items(grid_start, grid_end, today, kind, user_id)
    by_day: dict[str, list[dict]] = {}
    for item in items:
        by_day.setdefault(item["date"], []).append(item)

    weeks, day = [], grid_start
    while day <= grid_end:
        week = []
        for _ in range(7):
            day_items = by_day.get(day.isoformat(), [])
            week.append({
                "date": day.isoformat(), "day": day.day,
                "in_month": day.month == month_start.month,
                "is_today": day == today, "is_weekend": day.weekday() >= 5,
                "items": day_items[:MAX_CHIPS_PER_DAY], "more": max(0, len(day_items) - MAX_CHIPS_PER_DAY),
            })
            day += timedelta(days=1)
        weeks.append(week)

    in_month = [i for i in items if i["date"][:7] == month_start.strftime("%Y-%m")]
    agenda: list[dict] = []
    for item in in_month:
        if not agenda or agenda[-1]["date"] != item["date"]:
            d = date.fromisoformat(item["date"])
            agenda.append({"date": item["date"], "label": d.strftime("%a %d %b"), "is_today": d == today, "items": []})
        agenda[-1]["items"].append(item)

    week_ahead = _items(today, today + timedelta(days=6), today, user_id=user_id)
    prev_month = (month_start - timedelta(days=1)).replace(day=1)
    next_month = month_end + timedelta(days=1)
    return {
        "label": month_start.strftime("%B %Y"),
        "month": month_start.strftime("%Y-%m"),
        "prev": prev_month.strftime("%Y-%m"),
        "next": next_month.strftime("%Y-%m"),
        "this_month": today.strftime("%Y-%m"),
        "kind": kind,
        "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "weeks": weeks,
        "agenda": agenda,
        "counts": {
            "assessment": sum(1 for i in in_month if i["kind"] == "assessment"),
            "training": sum(1 for i in in_month if i["kind"] == "training"),
            "activity": sum(1 for i in in_month if i["kind"] == "activity"),
            "week_ahead": sum(1 for i in week_ahead if i["kind"] != "activity"),
            "people_in_training": len({i["user_id"] for i in in_month if i["kind"] == "training"}),
        },
    }


def upcoming_for_user(user_id: str, today: date, days: int = 180) -> list[dict]:
    """The employee's own bookings from today on (their assessment progress is not repeated here)."""
    return [i for i in _items(today, today + timedelta(days=days), today, user_id=user_id) if i["kind"] != "activity"]


def events_for_user(user_id: str) -> list[dict]:
    """Every booking for the employee, past and future (cancelled ones excluded)."""
    return [_event_item(r, date.today()) for r in db.list_events("0000-01-01", "9999-12-31", user_id=user_id)]


def month_events(month_start: date) -> list[dict]:
    """Every booking in one month (no assessment activity): what the scheduler exports."""
    _, days_in_month = calendar.monthrange(month_start.year, month_start.month)
    end = month_start.replace(day=days_in_month)
    return [_event_item(r, date.today()) for r in db.list_events(month_start.isoformat(), end.isoformat())]


def _ics_text(value) -> str:
    # RFC 5545 TEXT: escape backslash, semicolon and comma; a newline becomes the two characters \n,
    # so no value can start a new property line.
    text = str(value or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return re.sub(r"\r\n|\r|\n", lambda _: "\\n", text)  # a function: re would read "\\n" back as a newline


def _ics_fold(line: str) -> list[str]:
    out = []
    while len(line.encode("utf-8")) > 74:
        cut = 74
        while len(line[:cut].encode("utf-8")) > 74:
            cut -= 1
        out.append(line[:cut])
        line = " " + line[cut:]
    return out + [line]


def to_ics(items: list[dict], calendar_name: str, show_who: bool = True) -> str:
    """Bookings as an iCalendar file for Outlook / Google / Apple calendars. Cancelled and
    activity items are left out; a booking without a start time is an all-day event."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Talent 360i//Learning calendar//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{_ics_text(calendar_name)}"]
    for item in items:
        if item["kind"] == "activity" or item.get("status") == "cancelled":
            continue
        day = item["date"].replace("-", "")
        if item.get("time"):
            start = f"{day}T{item['time'].replace(':', '')}00"
            timing = [f"DTSTART:{start}", f"DURATION:PT{int(item.get('duration') or 60)}M"]
        else:
            nxt = (date.fromisoformat(item["date"]) + timedelta(days=1)).strftime("%Y%m%d")
            timing = [f"DTSTART;VALUE=DATE:{day}", f"DTEND;VALUE=DATE:{nxt}"]
        details = [item["kind_label"]] + ([item["who"]] if show_who and item.get("who") else []) + [
            item.get("detail") or "", item.get("notes") or ""]
        lines += ["BEGIN:VEVENT", f"UID:{_ics_text(item['id'])}@talent360i", f"DTSTAMP:{stamp}", *timing,
                  f"SUMMARY:{_ics_text(item['kind_label'] + ': ' + item['title'] + (' · ' + item['who'] if show_who and item.get('who') else ''))}",
                  f"DESCRIPTION:{_ics_text(chr(10).join(d for d in details if d))}",
                  "STATUS:CONFIRMED", "TRANSP:OPAQUE", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(folded for line in lines for folded in _ics_fold(line)) + "\r\n"


def employee_options() -> list[dict]:
    return [{"id": e["user_id"], "name": e["full_name"], "job_title": e["job_title"]} for e in db.list_employees()]


def training_options() -> list[dict]:
    """Every employee's open TNI gaps with their matched course, grouped per employee for the booking form."""
    groups = []
    for employee in db.list_employees():
        plan = tni.employee_tni(employee["user_id"])
        options = [
            {"value": f"{employee['user_id']}|{step['course']['course_id']}",
             "label": f"{step['skill']} · L{step['current']}→L{step['target']} · {step['course']['course_id']}"}
            for step in (plan or {}).get("path", []) if step["course"]
        ]
        if options:
            groups.append({"label": f"{employee['user_id']} · {employee['full_name']}", "options": options})
    return groups


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _employee(user_id: str) -> dict:
    user = db.get_user(user_id)
    if not user or user["role"] != "employee":
        raise ValueError("Choose an employee.")
    return user


def book_assessment(*, user_id: str, event_date: date, start_time: str | None, duration: int,
                    created_by: str, title: str | None = None) -> str:
    employee = _employee(user_id)
    plan = tni.employee_tni(user_id)
    role_name = plan["role_name"] if plan else employee.get("job_title") or "role"
    return db.create_event(
        event_type="assessment", title=title or f"Skill assessment · {role_name}",
        event_date=event_date.isoformat(), start_time=start_time, duration_minutes=duration,
        user_id=user_id, role_id=plan["role_id"] if plan else None, created_by=created_by,
    )


def book_training(*, user_id: str, course_id: str, event_date: date, start_time: str | None, duration: int,
                  created_by: str, note_prefix: str = "") -> str:
    """Books one recommended course; it must close one of this employee's open TNI gaps."""
    _employee(user_id)
    plan = tni.employee_tni(user_id)
    step = next((s for s in (plan or {}).get("path", []) if s["course"] and s["course"]["course_id"] == course_id), None)
    if not step:
        raise ValueError("That course is not recommended for any of this employee's open skill gaps.")
    if course_id in db.scheduled_course_ids(user_id):
        raise ValueError("This employee is already enrolled in that course.")
    return _create_training(plan, step, user_id, event_date, start_time, duration, created_by, note_prefix)


def _create_training(plan, step, user_id, event_date, start_time, duration, created_by, note_prefix="") -> str:
    return db.create_event(
        event_type="training", title=step["course"]["course_title"], event_date=event_date.isoformat(),
        start_time=start_time, duration_minutes=duration, user_id=user_id, role_id=plan["role_id"],
        skill=step["skill"], course_id=step["course"]["course_id"],
        notes=f"{note_prefix}TNI gap L{step['current']}→L{step['target']} ({step['severity']}) · est. {step['weeks']} wks to close",
        created_by=created_by,
    )


def auto_schedule_training(*, user_ids: list[str], start: date, per_employee: int, cadence_days: int,
                           start_time: str | None, duration: int, created_by: str, note_prefix: str = "") -> dict:
    """Books each employee's next recommended courses in skill-up order.

    Employees are staggered across the working week so one day isn't
    overloaded; each person's sessions then repeat every cadence_days.
    Courses already booked for that person are skipped, and a person who
    already has sessions booked continues one cadence after their last one,
    so re-running this books the next courses without double-booking a slot.
    """
    created, people = 0, 0
    for index, user_id in enumerate(user_ids):
        plan = tni.employee_tni(user_id)
        if not plan:
            continue
        booked = db.scheduled_course_ids(user_id)
        steps = [s for s in plan["path"] if s["course"] and s["course"]["course_id"] not in booked][:per_employee]
        if not steps:
            continue
        people += 1
        first_day = add_business_days(start, index % 5)
        last = db.last_training_date(user_id)
        if last and date.fromisoformat(last) >= first_day:
            first_day = add_business_days(date.fromisoformat(last) + timedelta(days=cadence_days), 0)
        for i, step in enumerate(steps):
            day = add_business_days(first_day + timedelta(days=i * cadence_days), 0)
            _create_training(plan, step, user_id, day, start_time, duration, created_by, note_prefix)
            created += 1
    return {"created": created, "people": people}


def book_cohort_assessment(*, title: str, event_date: date, duration: int, created_by: str) -> str:
    """The question-bank assessment window opened from the launch gate (not tied to one employee)."""
    return db.create_event(event_type="assessment", title=title, event_date=event_date.isoformat(),
                           duration_minutes=duration, notes="Cohort assessment window", created_by=created_by)


def cancel_event(event_id: str, today: date) -> bool:
    """Cancels a still-upcoming booking; past or already-cancelled ones are left as they are."""
    event = next((e for e in db.list_events(today.isoformat(), "9999-12-31") if e["event_id"] == event_id), None)
    return bool(event) and db.set_event_status(event_id, "cancelled")


def team_training(user_ids: set[str], today: date) -> list[dict]:
    """Training bookings for a set of employees (cancelled ones excluded), soonest first."""
    rows = [_event_item(r, today) for r in db.list_events("0000-01-01", "9999-12-31")
            if r["user_id"] in user_ids and r["event_type"] == "training"]
    for row in rows:
        row["can_complete"] = row["status"] == "scheduled" and row["date"] <= today.isoformat()
    return sorted(rows, key=lambda r: (r["status"] != "scheduled", r["date"], r["time"] or ""))


def complete_training(event_id: str, today: date) -> bool:
    """Marks a booked session as done -- only once its date has arrived."""
    event = db.get_event(event_id)
    if not event or event["event_type"] != "training" or event["event_date"] > today.isoformat():
        return False
    return db.set_event_status(event_id, "completed")


def mark_delivered(event_id: str, today: date) -> bool:
    """Records a training session as done today -- for one delivered earlier than it was booked."""
    event = db.get_event(event_id)
    if not event or event["event_type"] != "training" or event["status"] != "scheduled":
        return False
    return db.complete_event(event_id, min(event["event_date"], today.isoformat()))
