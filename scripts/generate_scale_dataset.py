"""
generate_scale_dataset.py

Builds a synthetic organisation of N employees (default 20,000) on top of the
real reference data -- the 9 roles, their skills, targets and the training
catalogue -- for load tests and the scale benchmark:

  - employees spread over the 9 roles, managers at about 1:10, 15 departments
    (5 per tower) each with its own leader, plus the demo role accounts
  - the TNI baseline: one skill-gap row per role skill per employee
  - per employee on average: 6 evidence items, 1 review, 3 development actions,
    2 pulse check-ins; about 8% new joiners on probation with check-ins

It writes a separate database (default db/scale_20k.sqlite) and never touches
the demo database. Deterministic: a fixed random seed. Takes well under a minute.

    python scripts/generate_scale_dataset.py --employees 20000
    python scripts/benchmark_scale.py --db db/scale_20k.sqlite
    TALENT360_DB_PATH=db/scale_20k.sqlite python start.py      # the portal on 20k people
"""

import argparse
import json
import os
import random
import sqlite3
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import bcrypt
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
PORTAL = ROOT / "Talent360i_phase11C_fixed_remote_hybrid"
SOURCE = ROOT / "db" / "talent360i.sqlite"
sys.path.insert(0, str(PORTAL))

FIRST = ("Aarav Aditi Akash Alicia Amara Ana Arjun Ben Bianca Carlos Chen Chloe Daniel Deepa Diego Elena Emma Farah "
         "Gabriel Grace Hana Hugo Ines Isaac Ishaan Jade James Jia Jonas Julia Kabir Kavya Kenji Lara Leo Lina Lucas "
         "Maya Meera Mia Mohammed Nadia Neha Nikhil Noah Olivia Omar Priya Rahul Raj Rhea Riya Rohan Sakura Samuel "
         "Sara Sofia Tanvi Theo Uma Vikram Wei Yara Yusuf Zara Zoe").split()
LAST = ("Agarwal Ahmed Alvarez Bauer Bose Chandra Chen Costa Das Dubois Edwards Fernandes Fischer Garcia Gupta Haddad "
        "Hughes Ito Iyer Jain Johnson Kapoor Kim Kumar Lee Lopez Malik Martin Mehta Menon Moreau Nair Nguyen Okafor "
        "Patel Pereira Pillai Rao Reddy Rossi Sato Schmidt Shah Sharma Silva Singh Smith Tanaka Thomas Verma Wang "
        "Weber Williams Wu Yadav Zhang").split()
CITIES = ("Pune", "Chicago", "Madrid", "Manila", "Kraków", "Bengaluru", "Toronto")
KINDS = (("appreciation", "positive", 30), ("achievement", "positive", 15), ("feedback", "neutral", 15),
         ("one_on_one", "neutral", 25), ("concern", "negative", 10), ("escalation", "negative", 5))
WIPE = ("assessment_answers", "generated_questions", "calibration_reviews", "skill_certifications", "training_outlines",
        "promotion_nominations", "scheduled_events", "assessment_cycles", "skill_gaps", "performance_evidence",
        "performance_reviews", "development_actions", "probation_checkpoints", "probation_records",
        "performance_escalations", "performance_insights", "employee_status", "employee_signals", "notifications", "pulse_checks",
        "employee_profiles", "departments", "job_profiles", "skill_requirement_changes", "coding_attempts",
        "cycle_resume_signals")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pick(rng, weighted):
    total = sum(w for *_, w in weighted)
    roll = rng.uniform(0, total)
    for *value, weight in weighted:
        roll -= weight
        if roll <= 0:
            return value if len(value) > 1 else value[0]
    return weighted[-1][:-1] if len(weighted[-1]) > 2 else weighted[-1][0]


def build(target: Path, employees: int, seed: int) -> dict:
    rng = random.Random(seed)
    today = date.today()
    if target.exists():
        target.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(target) + suffix).unlink(missing_ok=True)
    src = sqlite3.connect(SOURCE)
    dst = sqlite3.connect(target)
    src.backup(dst)  # consistent copy of the reference + demo data
    src.close()
    dst.close()

    from services import db, lifecycle_store as store
    db.DB_PATH = target
    store.ensure_schema()
    conn = sqlite3.connect(target)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    for table in WIPE:
        conn.execute(f"DELETE FROM {table}")  # nosec B608 -- fixed table names above
    conn.execute("DELETE FROM users WHERE role = 'employee' OR (role = 'manager' AND user_id != 'M001')")

    load_dotenv(ROOT / ".env")
    password = os.getenv("TALENT360_DEMO_PASSWORD") or uuid.uuid4().hex
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=10)).decode()

    roles = [dict(zip(("role_id", "tower", "role_name"), r)) for r in
             conn.execute("SELECT role_id, tower, role_name FROM role_master ORDER BY role_id")]
    skills_by_role: dict[str, list] = {}
    for r in conn.execute("SELECT role_id, skill_id, skill, capability, target_proficiency_level FROM role_skill_map"):
        skills_by_role.setdefault(r[0], []).append(r[1:])

    # Departments (5 per tower) and their leaders.
    departments = []
    users = []
    for tower in ("AP", "O2C", "R2R"):
        for n, city in enumerate(CITIES[:5], 1):
            dept_id = f"DEPT-{tower}-{n}"
            leader_id = f"LDR-{tower}-{n}"
            departments.append((dept_id, f"{tower} {city}", tower, f"{tower} operations, {city} hub", leader_id, "scale", _now()))
            users.append((leader_id, f"leader.{tower.lower()}{n}", f"leader.{tower.lower()}{n}@scale.local", pw_hash,
                          f"{rng.choice(FIRST)} {rng.choice(LAST)}", "leader", tower, "Department Leader", None))
    conn.executemany("INSERT INTO departments VALUES (?, ?, ?, ?, ?, ?, ?)", departments)
    depts_by_tower: dict[str, list] = {}
    for d in departments:
        depts_by_tower.setdefault(d[2], []).append(d[0])

    managers_needed = max(1, employees // 10)
    managers = [("M001", None)]
    for n in range(1, managers_needed):
        tower = ("AP", "O2C", "R2R")[n % 3]
        manager_id = f"MGR-{n:05d}"
        users.append((manager_id, f"mgr{n:05d}", f"mgr{n:05d}@scale.local", pw_hash, f"{rng.choice(FIRST)} {rng.choice(LAST)}",
                      "manager", tower, "Team Manager", None))
        managers.append((manager_id, tower))
    conn.executemany("INSERT INTO users (user_id, username, email, password_hash, full_name, role, department, job_title, "
                     "manager_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", users)

    people, gaps, profiles, evidence, reviews, actions, pulses, probations, checkpoints = ([] for _ in range(9))
    period = f"{today.year}-H1" if today.month > 6 else f"{today.year - 1}-H2"
    week = lambda d: f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"  # noqa: E731
    for n in range(1, employees + 1):
        uid = f"EMP-{n:06d}"
        manager_id, mtower = managers[(n - 1) // 10] if (n - 1) // 10 < len(managers) else rng.choice(managers)
        role = rng.choice([r for r in roles if not mtower or r["tower"] == mtower] or roles)
        dept = rng.choice(depts_by_tower[role["tower"]])
        people.append((uid, f"emp{n:06d}", f"emp{n:06d}@scale.local", pw_hash, f"{rng.choice(FIRST)} {rng.choice(LAST)}",
                       "employee", role["tower"], role["role_name"], manager_id))
        talent = rng.gauss(0, 0.6)  # one latent trait so skills, reviews and evidence agree, as they tend to
        for skill_id, skill, capability, level in skills_by_role[role["role_id"]]:
            current = max(1, min(5, round(level - 0.95 + 0.7 * talent + rng.gauss(0, 0.6))))
            gap = max(level - current, 0)
            gaps.append((f"G-{uid}-{skill_id}", uid, role["role_id"], "T", skill_id, skill, capability, level, float(current),
                         str(gap), "High" if gap >= 2 else "Moderate" if gap == 1 else "No gap", None, None, None))
        new_joiner = rng.random() < 0.08
        hired = today - timedelta(days=rng.randint(5, 175) if new_joiner else rng.randint(200, 3650))
        profiles.append((uid, hired.isoformat(), dept, rng.choice(CITIES), "permanent", _now()))
        for _ in range(max(0, round(rng.gauss(6, 3)))):
            kind, sentiment = _pick(rng, KINDS)
            if talent < -1.3 and rng.random() < 0.4:
                kind, sentiment = ("concern", "negative") if rng.random() < 0.7 else ("escalation", "negative")
            evidence.append((f"EVD-{uuid.uuid4().hex[:10].upper()}", uid, kind, "manual", f"{kind.replace('_', ' ').title()} note",
                             "Synthetic evidence for the scale benchmark.", (today - timedelta(days=rng.randint(0, 360))).isoformat(),
                             sentiment, rng.choice((1, 1, 1, 2, 3)), "[]", "confirmed", 1 if rng.random() < 0.92 else 0,
                             None, None, None, None, "[]", None, manager_id, _now(), manager_id, _now()))
        if not new_joiner and rng.random() < 0.85:
            rating = max(1, min(5, round(3.2 + 0.8 * talent + rng.gauss(0, 0.5))))
            reviews.append((f"REV-{uuid.uuid4().hex[:10].upper()}", uid, period, rating, "Synthetic review.", "", "",
                            "acknowledged" if rng.random() < 0.7 else "shared", manager_id, 0, None, _now(), _now(), _now(), None))
        for _ in range(rng.randint(0, 5)):
            due = today + timedelta(days=rng.randint(-60, 60))
            follow_through = min(0.95, max(0.25, 0.72 + 0.18 * talent))
            status = ("done" if rng.random() < follow_through else rng.choice(("open", "in_progress"))) if due < today \
                else rng.choice(("open", "in_progress"))
            actions.append((f"ACT-{uuid.uuid4().hex[:10].upper()}", uid, "Synthetic development action", None,
                            rng.choice(("training", "practice", "mentoring", "stretch", "behaviour")), None, None, "manager",
                            due.isoformat(), status, None, manager_id, (today - timedelta(days=rng.randint(10, 90))).isoformat(),
                            _now(), None))
        for back in range(rng.randint(0, 3)):
            pulses.append((f"PLS-{uuid.uuid4().hex[:10].upper()}", uid, week(today - timedelta(weeks=back)),
                           max(1, min(5, round(3.6 + 0.5 * talent + rng.gauss(0, 0.8)))), rng.randint(2, 5), "", 0, _now()))
        if new_joiner:
            probation_id = f"PRB-{uuid.uuid4().hex[:10].upper()}"
            probations.append((probation_id, uid, hired.isoformat(), (hired + timedelta(days=182)).isoformat(), "active", 0,
                               None, None, None, None, None, None, None, "[]", _now()))
            for label, days in (("Month 1 check-in", 30), ("Month 3 review", 90), ("Month 5 pre-decision review", 150)):
                due = hired + timedelta(days=days)
                done = due < today and rng.random() < 0.8
                rating = ("on_track" if talent > -0.5 else "at_risk") if done else None
                checkpoints.append((f"PCK-{uuid.uuid4().hex[:10].upper()}", probation_id, label, due.isoformat(), rating,
                                    "Synthetic check-in." if done else None, manager_id if done else None,
                                    _now() if done else None))

    conn.executemany("INSERT INTO users (user_id, username, email, password_hash, full_name, role, department, job_title, "
                     "manager_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", people)
    conn.execute("UPDATE users SET manager_id = NULL WHERE role = 'manager'")
    conn.executemany("INSERT INTO skill_gaps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", gaps)
    conn.executemany("INSERT INTO employee_profiles VALUES (?, ?, ?, ?, ?, ?)", profiles)
    conn.executemany("INSERT INTO performance_evidence (evidence_id, user_id, kind, source, title, summary, occurred_on, "
                     "sentiment, weight, skills_json, status, shared, origin_sender, excerpt, content_hash, ai_json, "
                     "action_items_json, employee_comment, created_by, created_at, decided_by, decided_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", evidence)
    conn.executemany("INSERT INTO performance_reviews (review_id, user_id, period, rating, summary, strengths, improvements, "
                     "status, manager_id, ai_assisted, employee_comment, created_at, updated_at, shared_at, acknowledged_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", reviews)
    conn.executemany("INSERT INTO development_actions (action_id, user_id, title, detail, action_type, skill, success_measure, "
                     "source, due_date, status, evidence_id, created_by, created_at, updated_at, completed_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", actions)
    conn.executemany("INSERT OR IGNORE INTO pulse_checks (pulse_id, user_id, week, mood, workload, comment, shared, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", pulses)
    conn.executemany("INSERT INTO probation_records (probation_id, user_id, start_date, end_date, status, extensions, "
                     "recommendation, recommendation_note, recommended_by, recommended_at, leader_id, leader_note, "
                     "decided_at, history_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", probations)
    conn.executemany("INSERT INTO probation_checkpoints (checkpoint_id, probation_id, label, due_date, rating, note, "
                     "completed_by, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", checkpoints)
    conn.commit()
    conn.execute("ANALYZE")
    conn.close()
    return {"employees": employees, "managers": len(managers), "leaders": len(departments), "departments": len(departments),
            "skill_gap_rows": len(gaps), "evidence": len(evidence), "reviews": len(reviews), "actions": len(actions),
            "pulses": len(pulses), "probations": len(probations), "db_mb": round(target.stat().st_size / 1e6, 1)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("--employees", type=int, default=20_000)
    parser.add_argument("--out", type=Path, default=ROOT / "db" / "scale_20k.sqlite")
    parser.add_argument("--seed", type=int, default=360)
    args = parser.parse_args()
    if args.out.resolve() == SOURCE.resolve():
        raise SystemExit("Refusing to overwrite the demo database.")
    started = time.perf_counter()
    stats = build(args.out, args.employees, args.seed)
    print(json.dumps({**stats, "seconds": round(time.perf_counter() - started, 1), "path": str(args.out)}, indent=2))


if __name__ == "__main__":
    main()
